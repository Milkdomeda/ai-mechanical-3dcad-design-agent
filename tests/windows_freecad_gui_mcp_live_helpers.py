from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Callable, Iterable, Mapping

import freecad_gui_mcp_provenance as provenance


APPROVED_FREECAD_VERSION = "1.1.3"
LIVE_OPT_IN = "MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS"
PREFLIGHT_OPT_IN = "MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_PREFLIGHT_TESTS"
REQUIRED_ENVIRONMENT = {
    "checkout": "MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT",
    "executable": "MECH_DESIGN_FREECAD_GUI_MCP_EXECUTABLE",
    "addon_path": "MECH_DESIGN_FREECAD_GUI_MCP_ADDON_PATH",
    "settings_path": "MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS",
    "freecad_exe": "MECH_DESIGN_FREECAD_EXE",
    "freecadcmd": "MECH_DESIGN_FREECADCMD",
    "release_root": "MECH_DESIGN_W4_ROOT",
}
_VERSION_PATTERN = re.compile(
    r"\bFreeCAD(?:Cmd)?\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WindowsHostFacts:
    system: str
    build: int
    machine: str
    python_version: tuple[int, int, int]
    python_architecture: str


@dataclass(frozen=True)
class WindowsLiveEnvironment:
    project_root: Path
    checkout: Path
    executable: Path
    addon_path: Path
    settings_path: Path
    freecad_exe: Path
    freecadcmd: Path
    release_root: Path
    host: WindowsHostFacts


@dataclass(frozen=True)
class ListenerRecord:
    local_address: str
    local_port: int
    owning_pid: int
    state: str


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    executable: Path


def live_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get(LIVE_OPT_IN, "").strip() == "1"


def preflight_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get(PREFLIGHT_OPT_IN, "").strip() == "1"


def current_host_facts() -> WindowsHostFacts:
    version_parts = [int(value) for value in re.findall(r"\d+", platform.version())]
    return WindowsHostFacts(
        system=platform.system(),
        build=version_parts[-1] if version_parts else 0,
        machine=platform.machine(),
        python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        python_architecture=platform.architecture()[0],
    )


def _assert_host(facts: WindowsHostFacts) -> None:
    if facts.system != "Windows" or facts.build < 22000:
        raise AssertionError("W4 acceptance requires Windows 11")
    if facts.machine.casefold() not in {"amd64", "x86_64"}:
        raise AssertionError("W4 acceptance requires Windows x64")
    if facts.python_version[:2] != (3, 12):
        raise AssertionError("W4 acceptance requires CPython 3.12")
    if facts.python_architecture != "64bit":
        raise AssertionError("W4 acceptance requires 64-bit Python")


def _default_validate_path(path: Path) -> Path:
    from mechanical_design_agent.secure_fs import validate_managed_path

    return validate_managed_path(path, allow_missing_leaf=False).path


def _default_require_x64_pe(path: Path) -> None:
    from mechanical_design_agent.freecad_discovery import _require_x64_pe

    _require_x64_pe(path)


def probe_freecad_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("FreeCAD version probe failed") from exc
    match = _VERSION_PATTERN.search(result.stdout or "")
    if result.returncode != 0 or match is None:
        raise AssertionError("FreeCAD version probe failed")
    return match.group(1)


def _inside(path: Path, root: Path) -> bool:
    return provenance.source_path_is_within(path, root)


def _same_path(left: Path, right: Path) -> bool:
    return provenance.same_source_path(left, right)


def require_live_environment(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
    *,
    host: WindowsHostFacts | None = None,
    validate_path: Callable[[Path], Path] = _default_validate_path,
    probe_freecad_version: Callable[[Path], str] = probe_freecad_version,
    require_x64_pe: Callable[[Path], None] = _default_require_x64_pe,
) -> WindowsLiveEnvironment:
    facts = current_host_facts() if host is None else host
    _assert_host(facts)
    values = os.environ if environ is None else environ
    raw: dict[str, Path] = {}
    for field, variable in REQUIRED_ENVIRONMENT.items():
        value = values.get(variable, "").strip()
        if not value:
            raise AssertionError(f"W4 acceptance requires explicit {variable}")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise AssertionError(f"{variable} must be an absolute path")
        try:
            raw[field] = validate_path(candidate)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AssertionError(
                f"{variable} must be on a safe local fixed NTFS path"
            ) from exc

    try:
        project = validate_path(Path(project_root))
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssertionError(
            "the W4 Candidate must be on a safe local fixed NTFS path"
        ) from exc
    for field in ("checkout", "addon_path", "release_root"):
        if not raw[field].is_dir():
            raise AssertionError(f"{REQUIRED_ENVIRONMENT[field]} must be a directory")
    for field in ("executable", "settings_path", "freecad_exe", "freecadcmd"):
        if not raw[field].is_file():
            raise AssertionError(f"{REQUIRED_ENVIRONMENT[field]} must be a file")
    if _inside(raw["checkout"], project):
        raise AssertionError("external checkout must remain outside the project")
    if _inside(raw["executable"], raw["checkout"]):
        raise AssertionError("server executable must use a separate environment")
    if _inside(raw["addon_path"], project):
        raise AssertionError("installed addon must remain outside the project")
    if _inside(raw["release_root"], project):
        raise AssertionError("W4 release root must remain outside the project")
    if raw["executable"].name.casefold() != "freecad-mcp.exe":
        raise AssertionError("W4 server executable must be freecad-mcp.exe")
    if raw["freecad_exe"].name.casefold() != "freecad.exe":
        raise AssertionError("W4 GUI executable must be FreeCAD.exe")
    if raw["freecadcmd"].name.casefold() != "freecadcmd.exe":
        raise AssertionError("W4 command executable must be FreeCADCmd.exe")
    if not _same_path(raw["freecad_exe"].parent, raw["freecadcmd"].parent):
        raise AssertionError("FreeCAD.exe and FreeCADCmd.exe must use the same official installation")
    require_x64_pe(raw["freecad_exe"])
    require_x64_pe(raw["freecadcmd"])
    version = probe_freecad_version(raw["freecadcmd"])
    if version != APPROVED_FREECAD_VERSION:
        raise AssertionError("W4 acceptance requires exact FreeCAD 1.1.3")
    return WindowsLiveEnvironment(project_root=project, host=facts, **raw)


def assert_external_provenance(
    env: WindowsLiveEnvironment,
    evidence: provenance.UpstreamEvidence | None = None,
) -> provenance.UpstreamEvidence:
    selected = provenance.assert_clean_checkout(env.checkout) if evidence is None else evidence
    provenance.assert_server_environment_matches_checkout(
        env.checkout,
        env.executable,
        selected,
    )
    provenance.assert_matching_addon(env.addon_path, selected)
    return selected


def query_tcp_listeners() -> tuple[ListenerRecord, ...]:
    script = (
        "Get-NetTCPConnection -State Listen -LocalPort 9875 | "
        "ForEach-Object { [pscustomobject]@{LocalAddress=$_.LocalAddress;"
        "LocalPort=$_.LocalPort;OwningProcess=$_.OwningProcess;"
        "State=[string]$_.State} } | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("Windows TCP listener inspection failed")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AssertionError("Windows TCP listener inspection returned invalid JSON") from exc
    rows = payload if isinstance(payload, list) else [payload]
    return tuple(
        ListenerRecord(
            local_address=str(row.get("LocalAddress", "")),
            local_port=int(row.get("LocalPort", 0)),
            owning_pid=int(row.get("OwningProcess", 0)),
            state=str(row.get("State", "")),
        )
        for row in rows
        if isinstance(row, dict)
    )


def query_process(pid: int) -> ProcessRecord:
    script = (
        f"Get-Process -Id {int(pid)} | Select-Object Id,Path | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("FreeCAD listener owner inspection failed")
    try:
        payload = json.loads(result.stdout)
        return ProcessRecord(int(payload["Id"]), Path(str(payload["Path"])).resolve(strict=True))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise AssertionError("FreeCAD listener owner inspection is invalid") from exc


def assert_local_rpc_security(
    env: WindowsLiveEnvironment,
    *,
    listeners: Iterable[ListenerRecord] | None = None,
    process_lookup: Callable[[int], ProcessRecord] = query_process,
) -> str:
    provenance.load_security_settings(env.settings_path)
    selected = tuple(query_tcp_listeners() if listeners is None else listeners)
    if (
        len(selected) != 1
        or selected[0].local_address != "127.0.0.1"
        or selected[0].local_port != 9875
        or selected[0].state.casefold() != "listen"
        or selected[0].owning_pid <= 0
    ):
        raise AssertionError("port 9875 must have the sole loopback listener")
    owner = process_lookup(selected[0].owning_pid)
    try:
        owner_path = _default_validate_path(owner.executable)
        approved_path = _default_validate_path(env.freecad_exe)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssertionError(
            "RPC listener owner or approved FreeCAD.exe could not be securely canonicalized"
        ) from exc
    if owner.pid != selected[0].owning_pid or not _same_path(owner_path, approved_path):
        raise AssertionError("RPC listener is not owned by the approved FreeCAD.exe")
    return "127.0.0.1:9875"


_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "platform",
        "windows_build",
        "architecture",
        "python_version",
        "freecad_version",
        "freecad_mcp_commit",
        "declared_project_version",
        "committed_lock_version",
        "evidence_digests",
        "server_tree_digest",
        "addon_tree_digest",
        "tool_name_digest",
        "endpoint",
        "workflow",
        "cleanup",
    }
)
_WORKFLOW_FIELDS = frozenset(
    {
        "initial_dimensions_mm",
        "initial_volume_mm3",
        "final_dimensions_mm",
        "final_volume_mm3",
        "screenshot_present",
        "screenshot_bytes",
        "screenshot_sha256",
        "manifest_schema",
        "shape_count",
        "geometry_count",
        "source_hash_unchanged",
    }
)
_CLEANUP_FIELDS = frozenset(
    {
        "synthetic_document_closed",
        "document_set_restored",
        "stdio_child_stopped",
        "attempt_files_removed",
        "upstream_unchanged",
        "addon_unchanged",
        "settings_unchanged",
        "repository_unchanged",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WINDOWS_ABSOLUTE = re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|\\\\)")
_NON_LOOPBACK_IPV4 = re.compile(
    r"\b(?!(?:127\.0\.0\.1)\b)(?:\d{1,3}\.){3}\d{1,3}\b"
)


def tree_digest(values: Mapping[str, str]) -> str:
    payload = "".join(f"{name}\0{digest}\n" for name, digest in sorted(values.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assert_evidence_privacy(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_evidence_privacy(key)
            _assert_evidence_privacy(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_evidence_privacy(item)
    elif isinstance(value, str):
        lowered = value.casefold()
        unsafe = (
            _WINDOWS_ABSOLUTE.search(value)
            or value.startswith(("/Users/", "/home/"))
            or _NON_LOOPBACK_IPV4.search(value)
            or re.search(r"[a-z][a-z0-9+.-]*://[^/\s]*@", value, re.IGNORECASE)
            or "internal-project-identifier" in lowered
        )
        if unsafe:
            raise AssertionError("safe evidence contains private or machine-specific data")


def validate_safe_evidence(evidence: dict[str, object]) -> dict[str, object]:
    if set(evidence) != _EVIDENCE_FIELDS:
        raise AssertionError("W4 evidence contains a forbidden or missing field")
    if evidence.get("endpoint") != "127.0.0.1:9875":
        raise AssertionError("W4 evidence endpoint must remain loopback-only")
    _assert_evidence_privacy(evidence)
    if evidence.get("schema_version") != "WindowsFreeCADGuiMcpEvidence/v1":
        raise AssertionError("W4 evidence schema is invalid")
    if evidence.get("platform") != "Windows 11":
        raise AssertionError("W4 evidence platform is invalid")
    if evidence.get("architecture") != "x64":
        raise AssertionError("W4 evidence architecture is invalid")
    if evidence.get("freecad_version") != APPROVED_FREECAD_VERSION:
        raise AssertionError("W4 evidence FreeCAD version is invalid")
    if evidence.get("freecad_mcp_commit") != provenance.APPROVED_COMMIT:
        raise AssertionError("W4 evidence upstream commit is invalid")
    if evidence.get("declared_project_version") != provenance.DECLARED_PROJECT_VERSION:
        raise AssertionError("W4 evidence declared version is invalid")
    if evidence.get("committed_lock_version") != provenance.COMMITTED_LOCK_VERSION:
        raise AssertionError("W4 evidence lock version is invalid")
    for field in ("server_tree_digest", "addon_tree_digest", "tool_name_digest"):
        if not isinstance(evidence.get(field), str) or not _SHA256.fullmatch(
            str(evidence[field])
        ):
            raise AssertionError(f"W4 evidence {field} is invalid")
    digests = evidence.get("evidence_digests")
    if not isinstance(digests, dict) or not digests or any(
        not isinstance(name, str)
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        for name, digest in digests.items()
    ):
        raise AssertionError("W4 evidence metadata digests are invalid")
    workflow = evidence.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != _WORKFLOW_FIELDS:
        raise AssertionError("W4 workflow evidence contains a forbidden or missing field")
    if workflow.get("initial_dimensions_mm") != [40.0, 30.0, 20.0]:
        raise AssertionError("W4 initial dimensions are invalid")
    if workflow.get("initial_volume_mm3") != 24000.0:
        raise AssertionError("W4 initial volume is invalid")
    if workflow.get("final_dimensions_mm") != [50.0, 30.0, 20.0]:
        raise AssertionError("W4 final dimensions are invalid")
    if workflow.get("final_volume_mm3") != 30000.0:
        raise AssertionError("W4 final volume is invalid")
    if workflow.get("screenshot_present") is not True or int(
        workflow.get("screenshot_bytes", 0)
    ) <= 0:
        raise AssertionError("W4 evidence requires a real non-empty screenshot")
    if not isinstance(workflow.get("screenshot_sha256"), str) or not _SHA256.fullmatch(
        str(workflow["screenshot_sha256"])
    ):
        raise AssertionError("W4 screenshot digest is invalid")
    if workflow.get("manifest_schema") != "ModelManifest/v2":
        raise AssertionError("W4 installed-wheel manifest evidence is invalid")
    if workflow.get("shape_count") != 1 or workflow.get("geometry_count") != 1:
        raise AssertionError("W4 extracted geometry counts are invalid")
    if workflow.get("source_hash_unchanged") is not True:
        raise AssertionError("W4 extraction changed the FCStd source")
    cleanup = evidence.get("cleanup")
    if not isinstance(cleanup, dict) or set(cleanup) != _CLEANUP_FIELDS:
        raise AssertionError("W4 cleanup evidence contains a forbidden or missing field")
    if any(cleanup.get(name) is not True for name in _CLEANUP_FIELDS):
        raise AssertionError("W4 cleanup failure overrides workflow success")
    return evidence
