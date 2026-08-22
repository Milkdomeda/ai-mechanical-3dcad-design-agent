from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
from typing import Mapping

import freecad_gui_mcp_provenance as provenance


APPROVED_COMMIT = provenance.APPROVED_COMMIT
APPROVED_REMOTE = provenance.APPROVED_REMOTE
KNOWN_COMPATIBLE_FREECAD_VERSIONS = frozenset({"1.1.1", "1.1.3"})
APPROVED_FREECAD_VERSION = "1.1.3"
DECLARED_PROJECT_VERSION = provenance.DECLARED_PROJECT_VERSION
COMMITTED_LOCK_VERSION = provenance.COMMITTED_LOCK_VERSION
AUDITED_SHA256 = provenance.AUDITED_SHA256
REQUIRED_ENVIRONMENT = {
    "checkout": "MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT",
    "executable": "MECH_DESIGN_FREECAD_GUI_MCP_EXECUTABLE",
    "addon_path": "MECH_DESIGN_FREECAD_GUI_MCP_ADDON_PATH",
    "settings_path": "MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS",
    "freecad_app": "MECH_DESIGN_FREECAD_APP",
    "freecadcmd": "MECH_DESIGN_FREECADCMD",
}
REQUIRED_TOOL_NAMES = provenance.REQUIRED_TOOL_NAMES
UpstreamEvidence = provenance.UpstreamEvidence


@dataclass(frozen=True)
class LiveEnvironment:
    project_root: Path
    checkout: Path
    executable: Path
    addon_path: Path
    settings_path: Path
    freecad_app: Path
    freecadcmd: Path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssertionError(f"{label} failed: {detail}")
    return result.stdout.strip()


def freecad_bundle_version(app: Path) -> str:
    info = app / "Contents" / "Info.plist"
    try:
        with info.open("rb") as stream:
            value = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise AssertionError("FreeCAD application bundle metadata is invalid") from exc
    version = value.get("CFBundleShortVersionString") or value.get("CFBundleVersion")
    if not isinstance(version, str) or not version.strip():
        raise AssertionError("FreeCAD application bundle has no version")
    return version.strip()


def require_live_environment(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
) -> LiveEnvironment:
    values = os.environ if environ is None else environ
    if platform.system() != "Darwin":
        raise AssertionError("live acceptance requires Darwin/macOS")
    raw: dict[str, Path] = {}
    for field, variable in REQUIRED_ENVIRONMENT.items():
        value = values.get(variable, "").strip()
        if not value:
            raise AssertionError(f"live acceptance requires explicit {variable}")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise AssertionError(f"{variable} must be an absolute path")
        try:
            raw[field] = candidate.resolve(strict=True)
        except OSError as exc:
            raise AssertionError(f"{variable} does not exist") from exc

    project = project_root.resolve(strict=True)
    if not raw["checkout"].is_dir():
        raise AssertionError("external checkout must be a directory")
    if _inside(raw["checkout"], project):
        raise AssertionError("external checkout must remain outside the project repository")
    if not raw["executable"].is_file():
        raise AssertionError("explicit FreeCAD GUI MCP executable is not a file")
    if _inside(raw["executable"], raw["checkout"]):
        raise AssertionError("FreeCAD GUI MCP executable must use a separate environment")
    if not raw["addon_path"].is_dir():
        raise AssertionError("explicit installed FreeCAD addon is not a directory")
    if _inside(raw["addon_path"], project):
        raise AssertionError("installed FreeCAD addon must remain outside the project repository")
    if not raw["settings_path"].is_file():
        raise AssertionError("explicit FreeCAD GUI MCP settings file is not a file")
    if not raw["freecad_app"].is_dir():
        raise AssertionError("FreeCAD application bundle is not a directory")
    if not raw["freecadcmd"].is_file():
        raise AssertionError("explicit FreeCADCmd is not a file")
    version = freecad_bundle_version(raw["freecad_app"])
    if version != APPROVED_FREECAD_VERSION:
        raise AssertionError(
            f"FreeCAD bundle version {version!r} is not {APPROVED_FREECAD_VERSION}"
        )
    return LiveEnvironment(project_root=project, **raw)


def assert_clean_checkout(env: LiveEnvironment) -> UpstreamEvidence:
    return provenance.assert_clean_checkout(
        env.checkout,
        approved_remote=APPROVED_REMOTE,
        approved_commit=APPROVED_COMMIT,
        declared_project_version=DECLARED_PROJECT_VERSION,
        committed_lock_version=COMMITTED_LOCK_VERSION,
        audited_sha256=AUDITED_SHA256,
    )


def _installed_server_payload(env: LiveEnvironment) -> dict[str, object]:
    return provenance.installed_server_payload(env.executable)


def assert_server_environment_matches_checkout(
    env: LiveEnvironment,
    evidence: UpstreamEvidence,
    payload: dict[str, object] | None = None,
) -> None:
    provenance.assert_server_environment_matches_checkout(
        env.checkout,
        env.executable,
        evidence,
        payload,
        declared_project_version=DECLARED_PROJECT_VERSION,
    )


def assert_matching_addon(env: LiveEnvironment, evidence: UpstreamEvidence) -> None:
    provenance.assert_matching_addon(
        env.addon_path,
        evidence,
        ignored_names={".DS_Store"},
    )


def _load_security_settings(path: Path) -> dict[str, object]:
    return provenance.load_security_settings(path)


def _listener_hosts(output: str) -> set[str]:
    hosts: set[str] = set()
    for line in output.splitlines():
        if "(LISTEN)" not in line:
            continue
        match = re.search(r"TCP\s+(\S+):9875\s+\(LISTEN\)", line)
        if match:
            hosts.add(match.group(1).strip("[]"))
    return hosts


def assert_local_rpc_security(env: LiveEnvironment) -> str:
    _load_security_settings(env.settings_path)
    result = _run(["lsof", "-nP", "-iTCP:9875", "-sTCP:LISTEN"])
    output = _require_success(result, "inspect FreeCAD RPC listener")
    hosts = _listener_hosts(output)
    if hosts != {"127.0.0.1"}:
        raise AssertionError(f"FreeCAD RPC listener is not loopback-only: {sorted(hosts)}")
    return "127.0.0.1:9875"


def repository_tree_state(root: Path) -> dict[str, tuple[int, int]]:
    prefixes = ("output", "knowledge", "data", "config", "vendor")
    state: dict[str, tuple[int, int]] = {}
    for prefix in prefixes:
        directory = root / prefix
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                state[path.relative_to(root).as_posix()] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                )
    return state


def assert_path_free_result(result: object, forbidden: set[str]) -> None:
    provenance.assert_path_free_result(result, forbidden)
