from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))
import freecad_gui_mcp_provenance as provenance  # noqa: E402
import windows_freecad_gui_mcp_live_helpers as windows_live  # noqa: E402
from windows_release_helpers import (  # noqa: E402
    build_release_artifacts,
    clean_release_environment,
    create_installed_wheel_environment,
    run_checked,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fixture_environment(tmp_path: Path) -> tuple[dict[str, str], dict[Path, Path]]:
    project = tmp_path / "agent"
    checkout = tmp_path / "external" / "freecad-mcp"
    server = tmp_path / "server-venv" / "Scripts" / "freecad-mcp.exe"
    addon = tmp_path / "appdata" / "FreeCAD" / "Mod" / "FreeCADMCP"
    settings = tmp_path / "appdata" / "FreeCAD" / "freecad_mcp_settings.json"
    freecad = tmp_path / "Program Files" / "FreeCAD 1.1.3" / "bin" / "FreeCAD.exe"
    freecadcmd = freecad.with_name("FreeCADCmd.exe")
    release_root = tmp_path / "release root"
    for directory in (
        project,
        checkout,
        server.parent,
        addon,
        settings.parent,
        freecad.parent,
        release_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for file in (server, freecad, freecadcmd):
        file.write_bytes(b"synthetic executable")
    settings.write_text(
        json.dumps({"remote_enabled": False, "allowed_ips": "127.0.0.1"}),
        encoding="utf-8",
    )
    values = {
        "MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT": str(checkout),
        "MECH_DESIGN_FREECAD_GUI_MCP_EXECUTABLE": str(server),
        "MECH_DESIGN_FREECAD_GUI_MCP_ADDON_PATH": str(addon),
        "MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS": str(settings),
        "MECH_DESIGN_FREECAD_EXE": str(freecad),
        "MECH_DESIGN_FREECADCMD": str(freecadcmd),
        "MECH_DESIGN_W4_ROOT": str(release_root),
    }
    canonical = {Path(value): Path(value).resolve() for value in values.values()}
    canonical[project] = project.resolve()
    canonical[PROJECT_ROOT] = PROJECT_ROOT.resolve()
    return values, canonical


def _host(**changes: object) -> windows_live.WindowsHostFacts:
    values: dict[str, object] = {
        "system": "Windows",
        "build": 26200,
        "machine": "AMD64",
        "python_version": (3, 12, 13),
        "python_architecture": "64bit",
    }
    values.update(changes)
    return windows_live.WindowsHostFacts(**values)


def _environment(tmp_path: Path, **host_changes: object) -> windows_live.WindowsLiveEnvironment:
    values, canonical = _fixture_environment(tmp_path)
    return windows_live.require_live_environment(
        PROJECT_ROOT,
        values,
        host=_host(**host_changes),
        validate_path=lambda path: canonical[Path(path)],
        probe_freecad_version=lambda _path: "1.1.3",
        require_x64_pe=lambda _path: None,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"system": "Darwin"}, "Windows 11"),
        ({"build": 19045}, "Windows 11"),
        ({"machine": "ARM64"}, "x64"),
        ({"python_version": (3, 11, 9)}, "CPython 3.12"),
        ({"python_architecture": "32bit"}, "64-bit"),
    ],
)
def test_windows_host_preflight_fails_closed(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(AssertionError, match=message):
        _environment(tmp_path, **changes)


def test_windows_preflight_requires_every_explicit_path(tmp_path: Path) -> None:
    values, canonical = _fixture_environment(tmp_path)
    values.pop("MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS")
    with pytest.raises(AssertionError, match="MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS"):
        windows_live.require_live_environment(
            PROJECT_ROOT,
            values,
            host=_host(),
            validate_path=lambda path: canonical[Path(path)],
            probe_freecad_version=lambda _path: "1.1.3",
            require_x64_pe=lambda _path: None,
        )


def test_windows_preflight_rejects_unsafe_path_and_wrong_freecad_version(
    tmp_path: Path,
) -> None:
    values, canonical = _fixture_environment(tmp_path)

    def reject_addon(path: Path) -> Path:
        if Path(path) == Path(values["MECH_DESIGN_FREECAD_GUI_MCP_ADDON_PATH"]):
            raise ValueError("WINDOWS_REPARSE_POINT_BLOCKED")
        return canonical[Path(path)]

    with pytest.raises(AssertionError, match="safe local fixed NTFS"):
        windows_live.require_live_environment(
            PROJECT_ROOT,
            values,
            host=_host(),
            validate_path=reject_addon,
            probe_freecad_version=lambda _path: "1.1.3",
            require_x64_pe=lambda _path: None,
        )

    with pytest.raises(AssertionError, match="exact FreeCAD 1.1.3"):
        windows_live.require_live_environment(
            PROJECT_ROOT,
            values,
            host=_host(),
            validate_path=lambda path: canonical[Path(path)],
            probe_freecad_version=lambda _path: "1.1.1",
            require_x64_pe=lambda _path: None,
        )


def test_windows_preflight_rejects_mismatched_freecad_install_and_project_paths(
    tmp_path: Path,
) -> None:
    values, canonical = _fixture_environment(tmp_path)
    other = tmp_path / "other" / "FreeCADCmd.exe"
    other.parent.mkdir()
    other.write_bytes(b"fixture")
    values["MECH_DESIGN_FREECADCMD"] = str(other)
    canonical[other] = other.resolve()
    with pytest.raises(AssertionError, match="same official installation"):
        windows_live.require_live_environment(
            PROJECT_ROOT,
            values,
            host=_host(),
            validate_path=lambda path: canonical[Path(path)],
            probe_freecad_version=lambda _path: "1.1.3",
            require_x64_pe=lambda _path: None,
        )

    values["MECH_DESIGN_FREECADCMD"] = str(
        Path(values["MECH_DESIGN_FREECAD_EXE"]).with_name("FreeCADCmd.exe")
    )
    project = Path(values["MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT"]).parents[1] / "agent"
    project_checkout = project / "inside-checkout"
    project_checkout.mkdir()
    values["MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT"] = str(project_checkout)
    canonical[project_checkout] = project_checkout
    with pytest.raises(AssertionError, match="outside the project"):
        windows_live.require_live_environment(
            project,
            values,
            host=_host(),
            validate_path=lambda path: canonical[Path(path)],
            probe_freecad_version=lambda _path: "1.1.3",
            require_x64_pe=lambda _path: None,
        )


def test_rpc_listener_requires_single_loopback_owner_from_approved_freecad(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    listener = windows_live.ListenerRecord("127.0.0.1", 9875, 42, "Listen")
    assert windows_live.assert_local_rpc_security(
        env,
        listeners=(listener,),
        process_lookup=lambda _pid: windows_live.ProcessRecord(42, env.freecad_exe),
    ) == "127.0.0.1:9875"

    for bad_listeners in (
        (),
        (replace(listener, local_address="0.0.0.0"),),
        (replace(listener, local_address="::"),),
        (listener, replace(listener, owning_pid=43)),
    ):
        with pytest.raises(AssertionError, match="sole loopback listener"):
            windows_live.assert_local_rpc_security(
                env,
                listeners=bad_listeners,
                process_lookup=lambda _pid: windows_live.ProcessRecord(
                    42, env.freecad_exe
                ),
            )

    with pytest.raises(AssertionError, match="approved FreeCAD.exe"):
        windows_live.assert_local_rpc_security(
            env,
            listeners=(listener,),
            process_lookup=lambda _pid: windows_live.ProcessRecord(
                42, tmp_path / "other" / "FreeCAD.exe"
            ),
        )


def test_rpc_listener_owner_accepts_equivalent_extended_windows_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = replace(
        _environment(tmp_path),
        freecad_exe=Path(r"C:\Program Files\FreeCAD 1.1.3\bin\FreeCAD.exe"),
    )
    monkeypatch.setattr(
        windows_live,
        "_default_validate_path",
        lambda _path: Path(
            r"\\?\C:\Program Files\FreeCAD 1.1.3\bin\FreeCAD.exe"
        ),
    )
    listener = windows_live.ListenerRecord("127.0.0.1", 9875, 42, "Listen")

    assert windows_live.assert_local_rpc_security(
        env,
        listeners=(listener,),
        process_lookup=lambda _pid: windows_live.ProcessRecord(
            42,
            Path(r"C:\Program Files\FreeCAD 1.1.3\bin\FreeCAD.exe"),
        ),
    ) == "127.0.0.1:9875"


def test_windows_provenance_reuses_common_exact_upstream_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _environment(tmp_path)
    evidence = provenance.UpstreamEvidence(
        commit=provenance.APPROVED_COMMIT,
        declared_project_version="0.1.19",
        committed_lock_version="0.1.17",
        evidence_sha256=dict(provenance.AUDITED_SHA256),
        server_tree={"server.py": "a" * 64},
        addon_tree={"Init.py": "b" * 64},
    )
    observed: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        provenance,
        "assert_server_environment_matches_checkout",
        lambda checkout, executable, actual, **_kwargs: observed.append(
            (checkout, executable)
        )
        if actual is evidence
        else None,
    )
    monkeypatch.setattr(provenance, "assert_matching_addon", lambda path, actual: None)
    windows_live.assert_external_provenance(env, evidence)
    assert observed == [(env.checkout, env.executable)]


def test_windows_extended_length_source_path_compares_to_direct_url_path() -> None:
    assert provenance.same_source_path(
        Path(r"\\?\C:\External\freecad-mcp"),
        Path(r"C:\External\freecad-mcp"),
    )
    assert not provenance.same_source_path(
        Path(r"\\?\C:\External\freecad-mcp"),
        Path(r"D:\External\freecad-mcp"),
    )
    assert provenance.source_path_is_within(
        Path(r"\\?\C:\External\freecad-mcp\src"),
        Path(r"C:\External\freecad-mcp"),
    )
    assert not provenance.source_path_is_within(
        Path(r"C:\External\freecad-mcp-other"),
        Path(r"\\?\C:\External\freecad-mcp"),
    )


def test_windows_settings_reject_remote_access_before_listener_probe(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    env.settings_path.write_text('{"remote_enabled": true}', encoding="utf-8")
    with pytest.raises(AssertionError, match="must be false"):
        windows_live.assert_local_rpc_security(
            env,
            listeners=(),
            process_lookup=lambda _pid: windows_live.ProcessRecord(1, env.freecad_exe),
        )


def test_immutable_preflight_rechecks_every_boundary_without_starting_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _environment(tmp_path)
    evidence = provenance.UpstreamEvidence(
        commit=provenance.APPROVED_COMMIT,
        declared_project_version=provenance.DECLARED_PROJECT_VERSION,
        committed_lock_version=provenance.COMMITTED_LOCK_VERSION,
        evidence_sha256=dict(provenance.AUDITED_SHA256),
        server_tree={"server.py": "a" * 64},
        addon_tree={"Init.py": "b" * 64},
    )
    repository_results = iter(("", ""))
    provenance_calls: list[windows_live.WindowsLiveEnvironment] = []
    monkeypatch.setattr(
        windows_live,
        "require_live_environment",
        lambda _root: env,
    )
    monkeypatch.setattr(
        windows_live,
        "assert_external_provenance",
        lambda actual: provenance_calls.append(actual) or evidence,
    )
    monkeypatch.setattr(
        windows_live,
        "assert_local_rpc_security",
        lambda actual: "127.0.0.1:9875" if actual is env else "unexpected",
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_status",
        lambda: next(repository_results),
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_live_workflow",
        lambda *_args, **_kwargs: pytest.fail("immutable preflight started workflow"),
    )

    _immutable_preflight()

    assert provenance_calls == [env, env]


def test_real_windows_gate_routes_preflight_to_readonly_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(windows_live.PREFLIGHT_OPT_IN, "1")
    monkeypatch.delenv(windows_live.LIVE_OPT_IN, raising=False)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_immutable_preflight",
        lambda: calls.append("preflight"),
    )

    test_real_windows_gate_is_explicit_opt_in()

    assert calls == ["preflight"]


def _text_content(result: object) -> str:
    return "\n".join(
        item.text
        for item in getattr(result, "content", ())
        if getattr(item, "type", "") == "text"
    )


def _json_content(result: object) -> object:
    for item in getattr(result, "content", ()):
        if getattr(item, "type", "") != "text":
            continue
        try:
            return json.loads(item.text)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"tool response has no JSON content: {_text_content(result)}")


def _execution_json(result: object) -> dict[str, object]:
    marker = "MDA_W4_JSON:"
    text = _text_content(result)
    if marker not in text:
        raise AssertionError("execute_code response has no W4 JSON")
    payload = json.loads(text.split(marker, 1)[1].splitlines()[0])
    if not isinstance(payload, dict):
        raise AssertionError("execute_code W4 JSON must be an object")
    return payload


def _screenshot_evidence(result: object) -> tuple[int, str]:
    images = [
        item
        for item in getattr(result, "content", ())
        if getattr(item, "type", "") == "image"
    ]
    if len(images) != 1:
        raise AssertionError("W4 requires exactly one real screenshot image")
    raw = getattr(images[0], "data", "")
    try:
        payload = base64.b64decode(raw, validate=True) if isinstance(raw, str) else bytes(raw)
    except (ValueError, TypeError) as exc:
        raise AssertionError("W4 screenshot image is invalid") from exc
    if not payload:
        raise AssertionError("W4 screenshot image must be non-empty")
    return len(payload), hashlib.sha256(payload).hexdigest()


def test_screenshot_evidence_requires_real_nonempty_image() -> None:
    payload = b"synthetic screenshot bytes"
    result = SimpleNamespace(
        content=(SimpleNamespace(type="image", data=base64.b64encode(payload).decode()),)
    )
    assert _screenshot_evidence(result) == (
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )
    with pytest.raises(AssertionError, match="real screenshot"):
        _screenshot_evidence(
            SimpleNamespace(
                content=(SimpleNamespace(type="text", text="Cannot get screenshot"),)
            )
        )


def _inspection_code(document_name: str, object_name: str) -> str:
    return "\n".join(
        (
            "import FreeCAD, json",
            f"doc = FreeCAD.getDocument({document_name!r})",
            f"obj = doc.getObject({object_name!r})",
            "doc.recompute()",
            "box = obj.Shape.BoundBox",
            "value = {'dimensions_mm': [float(obj.Length), float(obj.Width), float(obj.Height)], "
            "'bbox_size_mm': [float(box.XLength), float(box.YLength), float(box.ZLength)], "
            "'solid_count': len(obj.Shape.Solids), 'volume_mm3': float(obj.Shape.Volume)}",
            "print('MDA_W4_JSON:' + json.dumps(value, sort_keys=True))",
        )
    )


def _assert_geometry(value: dict[str, object], length: float, volume: float) -> None:
    assert value == {
        "bbox_size_mm": [length, 30.0, 20.0],
        "dimensions_mm": [length, 30.0, 20.0],
        "solid_count": 1,
        "volume_mm3": volume,
    }


def _assert_installed_manifest(manifest: dict[str, object]) -> None:
    assert manifest["schema_version"] == "ModelManifest/v2"
    shapes = manifest["shape_definitions"]
    geometry = manifest["geometry_definitions"]
    assert isinstance(shapes, list) and len(shapes) == 1
    assert isinstance(geometry, list) and len(geometry) == 1
    assert shapes[0]["topology"]["solid_count"] == 1
    assert shapes[0]["bbox_mm"]["size"] == [50.0, 30.0, 20.0]
    assert shapes[0]["volume_mm3"] == 30000.0
    assert geometry[0]["bbox_size_mm"] == [50.0, 30.0, 20.0]


def test_installed_manifest_contract_rejects_wrong_geometry() -> None:
    manifest = {
        "schema_version": "ModelManifest/v2",
        "shape_definitions": [
            {
                "topology": {"solid_count": 1},
                "bbox_mm": {"size": [50.0, 30.0, 20.0]},
                "volume_mm3": 30000.0,
            }
        ],
        "geometry_definitions": [{"bbox_size_mm": [50.0, 30.0, 20.0]}],
    }
    _assert_installed_manifest(manifest)
    manifest["shape_definitions"][0]["volume_mm3"] = 1.0  # type: ignore[index]
    with pytest.raises(AssertionError):
        _assert_installed_manifest(manifest)


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict[str, object],
) -> object:
    async with asyncio.timeout(180):
        result = await session.call_tool(name, arguments)
    assert not result.isError, _text_content(result)
    return result


def _extract_with_installed_wheel(
    env: windows_live.WindowsLiveEnvironment,
    attempt_root: Path,
    source: Path,
) -> tuple[dict[str, object], bool]:
    outside = attempt_root / "outside repository"
    outside.mkdir()
    cache_value = os.environ.get("UV_CACHE_DIR", "").strip()
    assert cache_value, "W4 requires the Runbook-prepared UV cache"
    environment = clean_release_environment(
        attempt_root,
        uv_cache_dir=Path(cache_value),
    )
    wheel, _sdist = build_release_artifacts(
        project_root=PROJECT_ROOT,
        root=attempt_root,
        environment=environment,
        offline=True,
        safe_stage="W4_LIVE_OFFLINE_BUILD",
    )
    installed = create_installed_wheel_environment(
        wheel=wheel,
        root=attempt_root,
        outside=outside,
        environment=environment,
        offline=True,
        safe_venv_stage="W4_LIVE_OFFLINE_VENV",
        safe_install_stage="W4_LIVE_OFFLINE_PROJECT_INSTALL",
    )
    workspace = attempt_root / "extractor workspace"
    workspace.mkdir()
    family = workspace / "unused-family.json"
    family.write_text("{}\n", encoding="utf-8")
    manifest_path = workspace / "manifest.json"
    script = "\n".join(
        (
            "import json, sys",
            "from pathlib import Path",
            "from mechanical_design_agent.config import Settings",
            "from mechanical_design_agent.extractor import FreeCADExtractor",
            "workspace, source, output, freecadcmd, family = map(Path, sys.argv[1:])",
            "settings = Settings(workspace=workspace, package_root=workspace, database_url='', "
            "neo4j_uri='', neo4j_user='', neo4j_password='', freecadcmd=freecadcmd, "
            "actor_id='example-user', artifact_root=workspace / 'artifacts', family_config_path=family)",
            "manifest = FreeCADExtractor(settings).extract(source, output)",
            "print(json.dumps(manifest, sort_keys=True))",
        )
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = run_checked(
        [
            str(installed.python),
            "-I",
            "-c",
            script,
            str(workspace),
            str(source),
            str(manifest_path),
            str(env.freecadcmd),
            str(family),
        ],
        cwd=outside,
        environment=environment,
        timeout=900,
    )
    manifest = json.loads(result.stdout.splitlines()[-1])
    _assert_installed_manifest(manifest)
    return manifest, before == hashlib.sha256(source.read_bytes()).hexdigest()


def test_w4_installed_extraction_consumes_prepared_cache_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    prepared_cache = tmp_path / "prepared cache"
    prepared_cache.mkdir()
    source = attempt_root / "synthetic.FCStd"
    source.write_bytes(b"synthetic source")
    freecadcmd = tmp_path / "FreeCADCmd.exe"
    freecadcmd.write_bytes(b"fixture")
    monkeypatch.setenv("UV_CACHE_DIR", str(prepared_cache))
    observed: dict[str, object] = {}

    def fake_clean_release_environment(
        root: Path,
        *,
        uv_cache_dir: Path | None = None,
    ) -> dict[str, str]:
        assert root == attempt_root
        assert uv_cache_dir is not None
        assert uv_cache_dir.resolve() == prepared_cache.resolve()
        return {"UV_CACHE_DIR": str(prepared_cache.resolve())}

    def fake_build_release_artifacts(
        *,
        project_root: Path,
        root: Path,
        environment: dict[str, str],
        offline: bool = False,
        safe_stage: str | None = None,
    ) -> tuple[Path, Path]:
        observed["build"] = (project_root, root, dict(environment), offline, safe_stage)
        return root / "agent.whl", root / "agent.tar.gz"

    def fake_create_installed_wheel_environment(
        *,
        wheel: Path,
        root: Path,
        outside: Path,
        environment: dict[str, str],
        offline: bool = False,
        safe_venv_stage: str | None = None,
        safe_install_stage: str | None = None,
    ) -> SimpleNamespace:
        observed["install"] = (
            wheel,
            root,
            outside,
            dict(environment),
            offline,
            safe_venv_stage,
            safe_install_stage,
        )
        return SimpleNamespace(python=outside / "python.exe")

    manifest = {
        "schema_version": "ModelManifest/v2",
        "shape_definitions": [
            {
                "topology": {"solid_count": 1},
                "bbox_mm": {"size": [50.0, 30.0, 20.0]},
                "volume_mm3": 30000.0,
            }
        ],
        "geometry_definitions": [{"bbox_size_mm": [50.0, 30.0, 20.0]}],
    }

    def fake_run_checked(command: list[str], **kwargs: object):
        return subprocess.CompletedProcess(command, 0, json.dumps(manifest), "")

    monkeypatch.setattr(
        sys.modules[__name__],
        "clean_release_environment",
        fake_clean_release_environment,
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "build_release_artifacts",
        fake_build_release_artifacts,
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "create_installed_wheel_environment",
        fake_create_installed_wheel_environment,
    )
    monkeypatch.setattr(sys.modules[__name__], "run_checked", fake_run_checked)

    _manifest, unchanged = _extract_with_installed_wheel(
        SimpleNamespace(freecadcmd=freecadcmd),
        attempt_root,
        source,
    )

    assert unchanged is True
    build = observed["build"]
    install = observed["install"]
    assert build[2]["UV_CACHE_DIR"] == str(prepared_cache.resolve())
    assert build[3:] == (True, "W4_LIVE_OFFLINE_BUILD")
    assert install[3]["UV_CACHE_DIR"] == str(prepared_cache.resolve())
    assert install[4:] == (
        True,
        "W4_LIVE_OFFLINE_VENV",
        "W4_LIVE_OFFLINE_PROJECT_INSTALL",
    )


def _repository_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, "W4 repository integrity inspection failed"
    return result.stdout


def _immutable_preflight() -> None:
    repository_before = _repository_status()
    assert repository_before == "", "W4 requires a clean Candidate worktree"
    env = windows_live.require_live_environment(PROJECT_ROOT)
    evidence = windows_live.assert_external_provenance(env)
    addon_before = windows_live.tree_digest(evidence.addon_tree)
    settings_before = hashlib.sha256(env.settings_path.read_bytes()).hexdigest()
    assert windows_live.assert_local_rpc_security(env) == "127.0.0.1:9875"

    after = windows_live.assert_external_provenance(env)
    assert after == evidence, "external FreeCAD GUI MCP provenance changed during preflight"
    assert (
        windows_live.tree_digest(after.addon_tree) == addon_before
    ), "FreeCAD GUI MCP addon changed during preflight"
    assert (
        hashlib.sha256(env.settings_path.read_bytes()).hexdigest() == settings_before
    ), "FreeCAD GUI MCP settings changed during preflight"
    assert (
        _repository_status() == repository_before
    ), "Candidate changed during immutable preflight"


async def _live_workflow(
    env: windows_live.WindowsLiveEnvironment,
    evidence: provenance.UpstreamEvidence,
    attempt_root: Path,
) -> dict[str, object]:
    document_name = f"MDAW4_{uuid.uuid4().hex}"
    object_name = f"SyntheticBox_{uuid.uuid4().hex}"
    source = attempt_root / f"{uuid.uuid4().hex}.FCStd"
    initial_documents: set[str] | None = None
    final_documents: set[str] | None = None
    screenshot_bytes = 0
    screenshot_sha256 = ""
    manifest: dict[str, object] = {}
    source_unchanged = False
    child_stopped = False
    parameters = StdioServerParameters(
        command=str(env.executable),
        args=["--host", "127.0.0.1"],
        cwd=attempt_root,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH"
            and "PASSWORD" not in key.upper()
            and "DATABASE" not in key.upper()
            and "NEO4J" not in key.upper()
        },
    )
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                missing = provenance.REQUIRED_TOOL_NAMES - tool_names
                if missing:
                    raise AssertionError(f"required MCP tools are missing: {sorted(missing)}")
                listed = _json_content(await _call(session, "list_documents", {}))
                assert isinstance(listed, list)
                initial_documents = {str(name) for name in listed}
                try:
                    await _call(session, "create_document", {"name": document_name})
                    await _call(
                        session,
                        "create_object",
                        {
                            "doc_name": document_name,
                            "obj_type": "Part::Box",
                            "obj_name": object_name,
                            "obj_properties": {
                                "Length": 40.0,
                                "Width": 30.0,
                                "Height": 20.0,
                            },
                        },
                    )
                    assert _json_content(
                        await _call(
                            session,
                            "get_object",
                            {"doc_name": document_name, "obj_name": object_name},
                        )
                    ) is not None
                    initial = _execution_json(
                        await _call(
                            session,
                            "execute_code",
                            {"code": _inspection_code(document_name, object_name)},
                        )
                    )
                    _assert_geometry(initial, 40.0, 24000.0)
                    await _call(
                        session,
                        "edit_object",
                        {
                            "doc_name": document_name,
                            "obj_name": object_name,
                            "obj_properties": {"Length": 50.0},
                        },
                    )
                    final = _execution_json(
                        await _call(
                            session,
                            "execute_code",
                            {"code": _inspection_code(document_name, object_name)},
                        )
                    )
                    _assert_geometry(final, 50.0, 30000.0)
                    view = await _call(
                        session,
                        "get_view",
                        {
                            "view_name": "Isometric",
                            "width": 320,
                            "height": 240,
                            "focus_object": object_name,
                        },
                    )
                    screenshot_bytes, screenshot_sha256 = _screenshot_evidence(view)
                    save_code = "\n".join(
                        (
                            "import FreeCAD",
                            f"doc = FreeCAD.getDocument({document_name!r})",
                            "doc.recompute()",
                            f"doc.saveAs({str(source)!r})",
                        )
                    )
                    await _call(session, "execute_code", {"code": save_code})
                    assert source.is_file()
                    manifest, source_unchanged = await asyncio.to_thread(
                        _extract_with_installed_wheel,
                        env,
                        attempt_root,
                        source,
                    )
                    assert source_unchanged
                finally:
                    close_code = "\n".join(
                        (
                            "import FreeCAD",
                            f"name = {document_name!r}",
                            "if name in FreeCAD.listDocuments():",
                            "    FreeCAD.closeDocument(name)",
                        )
                    )
                    await _call(session, "execute_code", {"code": close_code})
                    final_list = _json_content(
                        await _call(session, "list_documents", {})
                    )
                    assert isinstance(final_list, list)
                    final_documents = {str(name) for name in final_list}
                    assert document_name not in final_documents
                    assert final_documents == initial_documents
                result = {
                    "schema_version": "WindowsFreeCADGuiMcpEvidence/v1",
                    "platform": "Windows 11",
                    "windows_build": env.host.build,
                    "architecture": "x64",
                    "python_version": ".".join(map(str, env.host.python_version)),
                    "freecad_version": windows_live.APPROVED_FREECAD_VERSION,
                    "freecad_mcp_commit": evidence.commit,
                    "declared_project_version": evidence.declared_project_version,
                    "committed_lock_version": evidence.committed_lock_version,
                    "evidence_digests": evidence.evidence_sha256,
                    "server_tree_digest": windows_live.tree_digest(evidence.server_tree),
                    "addon_tree_digest": windows_live.tree_digest(evidence.addon_tree),
                    "tool_name_digest": hashlib.sha256(
                        "\n".join(sorted(tool_names)).encode("utf-8")
                    ).hexdigest(),
                    "endpoint": "127.0.0.1:9875",
                    "workflow": {
                        "initial_dimensions_mm": [40.0, 30.0, 20.0],
                        "initial_volume_mm3": 24000.0,
                        "final_dimensions_mm": [50.0, 30.0, 20.0],
                        "final_volume_mm3": 30000.0,
                        "screenshot_present": True,
                        "screenshot_bytes": screenshot_bytes,
                        "screenshot_sha256": screenshot_sha256,
                        "manifest_schema": manifest["schema_version"],
                        "shape_count": len(manifest["shape_definitions"]),
                        "geometry_count": len(manifest["geometry_definitions"]),
                        "source_hash_unchanged": source_unchanged,
                    },
                }
        child_stopped = True
        result["cleanup"] = {
            "synthetic_document_closed": document_name not in (final_documents or set()),
            "document_set_restored": final_documents == initial_documents,
            "stdio_child_stopped": child_stopped,
            "attempt_files_removed": False,
            "upstream_unchanged": False,
            "addon_unchanged": False,
            "settings_unchanged": False,
            "repository_unchanged": False,
        }
        return result
    finally:
        # stdio_client owns only the child it launched. FreeCAD GUI is never stopped.
        pass


def test_real_windows_gate_is_explicit_opt_in() -> None:
    if sys.platform != "win32":
        pytest.skip("native Windows W4 gate runs only on the certification host")
    preflight = windows_live.preflight_opted_in()
    live = windows_live.live_opted_in()
    if preflight and live:
        raise AssertionError("W4 preflight and live mutation opt-ins are mutually exclusive")
    if preflight:
        _immutable_preflight()
        return
    if not live:
        pytest.skip("set MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS=1")
    repository_before = _repository_status()
    assert repository_before == "", "W4 requires a clean Candidate worktree"
    env = windows_live.require_live_environment(PROJECT_ROOT)
    evidence = windows_live.assert_external_provenance(env)
    endpoint = windows_live.assert_local_rpc_security(env)
    assert endpoint == "127.0.0.1:9875"
    addon_before = windows_live.tree_digest(evidence.addon_tree)
    settings_before = hashlib.sha256(env.settings_path.read_bytes()).hexdigest()
    attempt_path: Path | None = None
    result: dict[str, object] | None = None
    cleanup_error: Exception | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="w4-freecad-gui-mcp-",
            dir=env.release_root,
        ) as value:
            attempt_path = Path(value)
            result = asyncio.run(_live_workflow(env, evidence, attempt_path))
    except Exception as exc:
        cleanup_error = exc
    attempt_removed = attempt_path is not None and not attempt_path.exists()
    try:
        after = windows_live.assert_external_provenance(env)
        upstream_unchanged = after == evidence
        addon_unchanged = windows_live.tree_digest(after.addon_tree) == addon_before
        settings_unchanged = (
            hashlib.sha256(env.settings_path.read_bytes()).hexdigest()
            == settings_before
        )
        repository_unchanged = _repository_status() == repository_before
    except Exception as exc:
        cleanup_error = cleanup_error or exc
        upstream_unchanged = addon_unchanged = settings_unchanged = False
        repository_unchanged = False
    if result is not None:
        cleanup = result["cleanup"]
        assert isinstance(cleanup, dict)
        cleanup.update(
            attempt_files_removed=attempt_removed,
            upstream_unchanged=upstream_unchanged,
            addon_unchanged=addon_unchanged,
            settings_unchanged=settings_unchanged,
            repository_unchanged=repository_unchanged,
        )
    if cleanup_error is not None:
        raise cleanup_error
    assert result is not None
    windows_live.validate_safe_evidence(result)
    print("W4_SAFE_EVIDENCE=" + json.dumps(result, sort_keys=True))
