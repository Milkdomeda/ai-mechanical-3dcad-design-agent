from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tomllib
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

import freecad_gui_mcp_provenance as provenance
import freecad_gui_mcp_live_helpers as live


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE_OPT_IN = "MECH_DESIGN_FREECAD_GUI_MCP_LIVE_TESTS"
LIVE_SKIP_REASON = (
    "set MECH_DESIGN_FREECAD_GUI_MCP_LIVE_TESTS=1 for the isolated macOS "
    "FreeCAD GUI MCP release E2E"
)


def test_platform_neutral_provenance_contract_is_shared() -> None:
    assert live.APPROVED_COMMIT == provenance.APPROVED_COMMIT
    assert live.APPROVED_REMOTE == provenance.APPROVED_REMOTE
    assert live.DECLARED_PROJECT_VERSION == provenance.DECLARED_PROJECT_VERSION
    assert live.COMMITTED_LOCK_VERSION == provenance.COMMITTED_LOCK_VERSION
    assert live.AUDITED_SHA256 == provenance.AUDITED_SHA256
    assert live.REQUIRED_TOOL_NAMES is provenance.REQUIRED_TOOL_NAMES
    assert live.UpstreamEvidence is provenance.UpstreamEvidence


def test_freecad_version_policy_separates_historical_from_release_approval() -> None:
    assert live.KNOWN_COMPATIBLE_FREECAD_VERSIONS == frozenset({"1.1.1", "1.1.3"})
    assert live.APPROVED_FREECAD_VERSION == "1.1.3"


def _write_bundle(app: Path, version: str = "1.1.3") -> None:
    info = app / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    with info.open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": version}, stream)


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fake_live_environment(tmp_path: Path, monkeypatch) -> live.LiveEnvironment:
    project = tmp_path / "project"
    checkout = tmp_path / "external" / "freecad-mcp"
    executable = tmp_path / "server-env" / "bin" / "freecad-mcp"
    python = executable.parent / "python3"
    addon = tmp_path / "FreeCAD-user" / "Mod" / "FreeCADMCP"
    settings = tmp_path / "FreeCAD-user" / "freecad_mcp_settings.json"
    app = tmp_path / "Applications" / "FreeCAD.app"
    freecadcmd = app / "Contents" / "Resources" / "bin" / "FreeCADCmd"
    project.mkdir()
    (checkout / "src" / "freecad_mcp").mkdir(parents=True)
    (checkout / "addon" / "FreeCADMCP").mkdir(parents=True)
    (checkout / "LICENSE").write_text("MIT fixture\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "freecad-mcp"\nversion = "0.1.19"\n',
        encoding="utf-8",
    )
    (checkout / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "freecad-mcp"\nversion = "0.1.17"\n',
        encoding="utf-8",
    )
    (checkout / "src" / "freecad_mcp" / "server.py").write_bytes(b"VALUE = 1\n")
    addon_source = checkout / "addon" / "FreeCADMCP" / "Init.py"
    addon_source.write_bytes(b"VALUE = 2\n")
    addon.mkdir(parents=True)
    (addon / "Init.py").write_bytes(addon_source.read_bytes())
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        '{"remote_enabled": false, "allowed_ips": "127.0.0.1"}\n',
        encoding="utf-8",
    )
    _write_bundle(app)
    freecadcmd.parent.mkdir(parents=True)
    freecadcmd.write_text("fixture\n", encoding="utf-8")

    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "release-test@example.invalid")
    _git(checkout, "config", "user.name", "Release Test")
    _git(checkout, "remote", "add", "origin", live.APPROVED_REMOTE)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "fixture")
    monkeypatch.setattr(live, "APPROVED_COMMIT", _git(checkout, "rev-parse", "HEAD"))
    monkeypatch.setattr(
        live,
        "AUDITED_SHA256",
        {
            name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
            for name in ("LICENSE", "pyproject.toml", "uv.lock")
        },
    )
    values = {
        live.REQUIRED_ENVIRONMENT["checkout"]: str(checkout),
        live.REQUIRED_ENVIRONMENT["executable"]: str(executable),
        live.REQUIRED_ENVIRONMENT["addon_path"]: str(addon),
        live.REQUIRED_ENVIRONMENT["settings_path"]: str(settings),
        live.REQUIRED_ENVIRONMENT["freecad_app"]: str(app),
        live.REQUIRED_ENVIRONMENT["freecadcmd"]: str(freecadcmd),
    }
    monkeypatch.setattr(live.platform, "system", lambda: "Darwin")
    return live.require_live_environment(project, values)


def test_preflight_rejects_wrong_platform(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(live.platform, "system", lambda: "Windows")
    with pytest.raises(AssertionError, match="Darwin/macOS"):
        live.require_live_environment(project, {})


def test_preflight_rejects_wrong_freecad_bundle_version(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    _write_bundle(env.freecad_app, "1.1.0")
    values = {
        variable: str(getattr(env, field))
        for field, variable in live.REQUIRED_ENVIRONMENT.items()
    }
    with pytest.raises(AssertionError, match="is not 1.1.3"):
        live.require_live_environment(env.project_root, values)


def test_preflight_rejects_repository_checkout_and_in_checkout_executable(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    contained = env.project_root / "external"
    contained.mkdir()
    values = {
        variable: str(getattr(env, field))
        for field, variable in live.REQUIRED_ENVIRONMENT.items()
    }
    values[live.REQUIRED_ENVIRONMENT["checkout"]] = str(contained)
    with pytest.raises(AssertionError, match="outside the project"):
        live.require_live_environment(env.project_root, values)

    inside_executable = env.checkout / "bin" / "freecad-mcp"
    inside_executable.parent.mkdir()
    inside_executable.write_text("fixture\n", encoding="utf-8")
    values[live.REQUIRED_ENVIRONMENT["checkout"]] = str(env.checkout)
    values[live.REQUIRED_ENVIRONMENT["executable"]] = str(inside_executable)
    with pytest.raises(AssertionError, match="separate environment"):
        live.require_live_environment(env.project_root, values)


def test_clean_checkout_evidence_preserves_two_upstream_versions(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    evidence = live.assert_clean_checkout(env)
    assert evidence.declared_project_version == "0.1.19"
    assert evidence.committed_lock_version == "0.1.17"
    assert evidence.server_tree == {
        "server.py": hashlib.sha256(b"VALUE = 1\n").hexdigest()
    }
    assert evidence.addon_tree == {
        "Init.py": hashlib.sha256(b"VALUE = 2\n").hexdigest()
    }


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_clean_checkout_rejects_dirty_or_untracked_files(
    tmp_path: Path, monkeypatch, dirty_kind: str
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    if dirty_kind == "tracked":
        (env.checkout / "LICENSE").write_text("changed\n", encoding="utf-8")
    else:
        (env.checkout / "untracked.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="clean index and worktree"):
        live.assert_clean_checkout(env)


def test_clean_checkout_rejects_wrong_head_hash_and_normalized_versions(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "APPROVED_COMMIT", "0" * 40)
    with pytest.raises(AssertionError, match="approved commit"):
        live.assert_clean_checkout(env)

    monkeypatch.setattr(live, "APPROVED_COMMIT", _git(env.checkout, "rev-parse", "HEAD"))
    monkeypatch.setattr(live, "AUDITED_SHA256", {**live.AUDITED_SHA256, "LICENSE": "0" * 64})
    with pytest.raises(AssertionError, match="audited file hashes"):
        live.assert_clean_checkout(env)

    lock = env.checkout / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8").replace("0.1.17", "0.1.19"), encoding="utf-8")
    _git(env.checkout, "add", "uv.lock")
    _git(env.checkout, "commit", "-qm", "normalize fixture")
    monkeypatch.setattr(live, "APPROVED_COMMIT", _git(env.checkout, "rev-parse", "HEAD"))
    monkeypatch.setattr(
        live,
        "AUDITED_SHA256",
        {
            name: hashlib.sha256((env.checkout / name).read_bytes()).hexdigest()
            for name in ("LICENSE", "pyproject.toml", "uv.lock")
        },
    )
    with pytest.raises(AssertionError, match="must remain distinct"):
        live.assert_clean_checkout(env)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"version": "0.1.17"}, "declared fact"),
        ({"direct_url": None}, "direct_url provenance"),
        ({"direct_url": {"url": "file:///tmp/another", "dir_info": {}}}, "does not exist"),
        ({"direct_url": {"url": "PLACEHOLDER", "dir_info": {"editable": True}}}, "editable"),
        ({"files": {}}, "source differs"),
    ],
)
def test_server_provenance_fails_closed(
    tmp_path: Path, monkeypatch, change: dict[str, object], message: str
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    evidence = live.assert_clean_checkout(env)
    payload: dict[str, object] = {
        "version": "0.1.19",
        "direct_url": {
            "url": env.checkout.as_uri(),
            "dir_info": {},
        },
        "files": evidence.server_tree,
    }
    payload.update(change)
    direct = payload.get("direct_url")
    if isinstance(direct, dict) and direct.get("url") == "PLACEHOLDER":
        direct["url"] = env.checkout.as_uri()
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if not change:
        live.assert_server_environment_matches_checkout(env, evidence, payload)
    else:
        with pytest.raises((AssertionError, FileNotFoundError), match=message):
            live.assert_server_environment_matches_checkout(env, evidence, payload)


def test_server_provenance_accepts_exact_noneditable_install(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    evidence = live.assert_clean_checkout(env)
    payload = {
        "version": "0.1.19",
        "direct_url": {
            "url": env.checkout.as_uri(),
            "dir_info": {},
        },
        "files": evidence.server_tree,
    }
    monkeypatch.delenv("PYTHONPATH", raising=False)
    live.assert_server_environment_matches_checkout(env, evidence, payload)


def test_addon_correspondence_rejects_changed_and_extra_source(
    tmp_path: Path, monkeypatch
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    evidence = live.assert_clean_checkout(env)
    live.assert_matching_addon(env, evidence)
    (env.addon_path / "Init.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="differs"):
        live.assert_matching_addon(env, evidence)
    (env.addon_path / "Init.py").write_bytes(b"VALUE = 2\n")
    (env.addon_path / "extra.py").write_text("extra\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="untracked files"):
        live.assert_matching_addon(env, evidence)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("not-json", "invalid JSON"),
        ('{"remote_enabled": true}', "must be false"),
        ('{"remote_enabled": false, "allowed_ips": "192.168.1.0/24"}', "loopback-only"),
    ],
)
def test_security_settings_fail_closed(
    tmp_path: Path, monkeypatch, settings: str, message: str
) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    env.settings_path.write_text(settings, encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        live.assert_local_rpc_security(env)


def test_rpc_listener_must_be_loopback_only(tmp_path: Path, monkeypatch) -> None:
    env = _fake_live_environment(tmp_path, monkeypatch)
    safe = subprocess.CompletedProcess(
        ["lsof"], 0, "FreeCAD 1 user 1 TCP 127.0.0.1:9875 (LISTEN)\n", ""
    )
    monkeypatch.setattr(live, "_run", lambda *_args, **_kwargs: safe)
    assert live.assert_local_rpc_security(env) == "127.0.0.1:9875"
    unsafe = subprocess.CompletedProcess(
        ["lsof"], 0, "FreeCAD 1 user 1 TCP *:9875 (LISTEN)\n", ""
    )
    monkeypatch.setattr(live, "_run", lambda *_args, **_kwargs: unsafe)
    with pytest.raises(AssertionError, match="not loopback-only"):
        live.assert_local_rpc_security(env)


def _text_content(result) -> str:
    return "\n".join(
        item.text for item in result.content if getattr(item, "type", "") == "text"
    )


def _json_content(result):
    for item in result.content:
        if getattr(item, "type", "") != "text":
            continue
        try:
            return json.loads(item.text)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"tool response has no JSON content: {_text_content(result)}")


def _execution_json(result) -> dict[str, object]:
    marker = "MDA_RELEASE_JSON:"
    text = _text_content(result)
    if marker not in text:
        raise AssertionError(f"execute_code response has no release JSON: {text}")
    encoded = text.split(marker, 1)[1].splitlines()[0]
    value = json.loads(encoded)
    assert isinstance(value, dict)
    return value


async def _call(session: ClientSession, name: str, arguments: dict[str, object]):
    async with asyncio.timeout(120):
        result = await session.call_tool(name, arguments)
    assert not result.isError, _text_content(result)
    return result


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
            "print('MDA_RELEASE_JSON:' + json.dumps(value, sort_keys=True))",
        )
    )


def _extract_installed_wheel(
    temporary: Path,
    env: live.LiveEnvironment,
    source: Path,
) -> tuple[dict[str, object], bool]:
    uv = shutil.which("uv")
    assert uv is not None
    dist = temporary / "dist"
    venv = temporary / "extractor-venv"
    outside = temporary / "outside-repository"
    workspace = temporary / "extractor-workspace"
    outside.mkdir()
    workspace.mkdir()
    family = workspace / "unused-family.json"
    family.write_text("{}\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["UV_CACHE_DIR"] = "/private/tmp/codex-uv-cache-portable-bootstrap"
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(dist.glob("*.whl"))
    created = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        cwd=temporary,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert created.returncode == 0, created.stderr
    python = venv / "bin" / "python"
    installed = subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stderr
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
    extracted = subprocess.run(
        [
            str(python),
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
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert extracted.returncode == 0, extracted.stderr
    manifest = json.loads(extracted.stdout.splitlines()[-1])
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    return manifest, before == after


async def _live_workflow(
    env: live.LiveEnvironment,
    evidence: live.UpstreamEvidence,
    temporary: Path,
) -> dict[str, object]:
    started = datetime.now(timezone.utc).isoformat()
    document_name = f"MDAReleaseBox_{uuid4().hex}"
    object_name = f"ReleaseBox_{uuid4().hex}"
    source = temporary / "synthetic.FCStd"
    initial_documents: set[str] | None = None
    final_documents: set[str] | None = None
    cleanup_confirmed = False
    parameters = StdioServerParameters(
        command=str(env.executable),
        args=["--host", "127.0.0.1"],
        cwd=temporary,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert live.REQUIRED_TOOL_NAMES <= tool_names
            initial_documents = set(
                _json_content(await _call(session, "list_documents", {}))
            )
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
                object_value = _json_content(
                    await _call(
                        session,
                        "get_object",
                        {"doc_name": document_name, "obj_name": object_name},
                    )
                )
                assert object_value is not None
                initial = _execution_json(
                    await _call(
                        session,
                        "execute_code",
                        {"code": _inspection_code(document_name, object_name)},
                    )
                )
                assert initial == {
                    "bbox_size_mm": [40.0, 30.0, 20.0],
                    "dimensions_mm": [40.0, 30.0, 20.0],
                    "solid_count": 1,
                    "volume_mm3": 24000.0,
                }
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
                assert final == {
                    "bbox_size_mm": [50.0, 30.0, 20.0],
                    "dimensions_mm": [50.0, 30.0, 20.0],
                    "solid_count": 1,
                    "volume_mm3": 30000.0,
                }
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
                assert any(
                    getattr(item, "type", "") == "image"
                    or "Cannot get screenshot" in getattr(item, "text", "")
                    for item in view.content
                )
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
                manifest, unchanged = await asyncio.to_thread(
                    _extract_installed_wheel, temporary, env, source
                )
                assert manifest["schema_version"] == "ModelManifest/v2"
                assert len(manifest["shape_definitions"]) == 1
                shape = manifest["shape_definitions"][0]
                assert shape["topology"]["solid_count"] == 1
                assert shape["bbox_mm"]["size"] == [50.0, 30.0, 20.0]
                assert shape["volume_mm3"] == 30000.0
                assert len(manifest["geometry_definitions"]) == 1
                assert manifest["geometry_definitions"][0]["bbox_size_mm"] == [
                    50.0,
                    30.0,
                    20.0,
                ]
                assert unchanged
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
                final_documents = set(
                    _json_content(await _call(session, "list_documents", {}))
                )
                cleanup_confirmed = (
                    document_name not in final_documents
                    and final_documents == initial_documents
                )
            assert cleanup_confirmed
    finished = datetime.now(timezone.utc).isoformat()
    return {
        "platform": "Darwin/macOS",
        "freecad_version": "1.1.3",
        "freecad_mcp_commit": evidence.commit,
        "declared_project_version": evidence.declared_project_version,
        "committed_lock_version": evidence.committed_lock_version,
        "evidence_digests": evidence.evidence_sha256,
        "tool_name_digest": hashlib.sha256(
            "\n".join(sorted(tool_names)).encode("utf-8")
        ).hexdigest(),
        "remote_enabled": False,
        "endpoint": "127.0.0.1:9875",
        "initial": initial,
        "final": final,
        "manifest_schema": manifest["schema_version"],
        "shape_count": len(manifest["shape_definitions"]),
        "geometry_count": len(manifest["geometry_definitions"]),
        "source_hash_unchanged": unchanged,
        "started_at": started,
        "finished_at": finished,
        "cleanup_confirmed": cleanup_confirmed,
    }


def test_freecad_gui_mcp_live_release_workflow(tmp_path: Path) -> None:
    if os.environ.get(LIVE_OPT_IN) != "1":
        pytest.skip(LIVE_SKIP_REASON)
    before_repository = live.repository_tree_state(PROJECT_ROOT)
    env = live.require_live_environment(PROJECT_ROOT)
    evidence = live.assert_clean_checkout(env)
    live.assert_server_environment_matches_checkout(env, evidence)
    live.assert_matching_addon(env, evidence)
    assert live.assert_local_rpc_security(env) == "127.0.0.1:9875"
    result = asyncio.run(_live_workflow(env, evidence, tmp_path))
    assert result["freecad_version"] == live.APPROVED_FREECAD_VERSION
    after_evidence = live.assert_clean_checkout(env)
    assert after_evidence == evidence
    assert live.repository_tree_state(PROJECT_ROOT) == before_repository
    forbidden = {
        str(PROJECT_ROOT),
        str(env.checkout),
        str(Path.home()),
        getpass.getuser(),
        socket.gethostname(),
    }
    live.assert_path_free_result(result, forbidden)
    assert result["cleanup_confirmed"] is True
