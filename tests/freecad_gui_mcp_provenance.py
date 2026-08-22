from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Mapping
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


APPROVED_COMMIT = "7667e272e1db669ff61dd5411fb4f622691f2dbc"
APPROVED_REMOTE = "https://github.com/neka-nat/freecad-mcp.git"
DECLARED_PROJECT_VERSION = "0.1.19"
COMMITTED_LOCK_VERSION = "0.1.17"
AUDITED_SHA256 = {
    "LICENSE": "396a409dd7ea20bb4f0bf2e2478daae4b8e28648d3c2d5b56f53b5b8715959c1",
    "pyproject.toml": "3dca251cb4f8c9a75a412bad06f693fb620b2810b46c40c761fc216280466573",
    "uv.lock": "08c615904101bc99a576982b741b5cb58e2d78a01f482e5f06a83d48d778af01",
}
REQUIRED_TOOL_NAMES = frozenset(
    {
        "list_documents",
        "create_document",
        "create_object",
        "get_object",
        "edit_object",
        "execute_code",
        "get_view",
    }
)


@dataclass(frozen=True)
class UpstreamEvidence:
    commit: str
    declared_project_version: str
    committed_lock_version: str
    evidence_sha256: dict[str, str]
    server_tree: dict[str, str]
    addon_tree: dict[str, str]


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
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssertionError(f"{label} failed: {detail}")
    return result.stdout.strip()


def _git(checkout: Path, *arguments: str) -> str:
    return _require_success(
        _run(["git", "-C", str(checkout), *arguments]),
        f"git {' '.join(arguments)}",
    )


def tracked_tree_digest(checkout: Path, prefix: str) -> dict[str, str]:
    listed = _git(checkout, "ls-files", "--", prefix)
    result: dict[str, str] = {}
    for relative in listed.splitlines():
        path = checkout / relative
        if not path.is_file() or path.is_symlink():
            raise AssertionError(f"tracked upstream file is not regular: {relative}")
        key = Path(relative).relative_to(prefix).as_posix()
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise AssertionError(f"tracked upstream tree is empty: {prefix}")
    return dict(sorted(result.items()))


def assert_clean_checkout(
    checkout: Path,
    *,
    approved_remote: str = APPROVED_REMOTE,
    approved_commit: str = APPROVED_COMMIT,
    declared_project_version: str = DECLARED_PROJECT_VERSION,
    committed_lock_version: str = COMMITTED_LOCK_VERSION,
    audited_sha256: Mapping[str, str] = AUDITED_SHA256,
) -> UpstreamEvidence:
    remote = _git(checkout, "remote", "get-url", "origin")
    if remote.rstrip("/") != approved_remote.rstrip("/"):
        raise AssertionError("external checkout origin is not the approved upstream")
    head = _git(checkout, "rev-parse", "HEAD")
    if head != approved_commit:
        raise AssertionError("external checkout is not at the approved commit")
    status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise AssertionError("external checkout must have a clean index and worktree")

    pyproject = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((checkout / "uv.lock").read_text(encoding="utf-8"))
    declared = str(pyproject.get("project", {}).get("version", ""))
    locked = next(
        (
            str(item.get("version", ""))
            for item in lock.get("package", [])
            if item.get("name") == "freecad-mcp"
        ),
        "",
    )
    if declared != declared_project_version or locked != committed_lock_version:
        raise AssertionError(
            "upstream version facts must remain distinct 0.1.19 and 0.1.17"
        )

    actual_hashes = {
        name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
        for name in audited_sha256
    }
    if actual_hashes != dict(audited_sha256):
        raise AssertionError("external checkout audited file hashes do not match")
    server_tree = {
        name: digest
        for name, digest in tracked_tree_digest(checkout, "src/freecad_mcp").items()
        if name.endswith(".py")
    }
    return UpstreamEvidence(
        commit=head,
        declared_project_version=declared,
        committed_lock_version=locked,
        evidence_sha256=actual_hashes,
        server_tree=server_tree,
        addon_tree=tracked_tree_digest(checkout, "addon/FreeCADMCP"),
    )


def _server_python(executable: Path) -> Path:
    for name in ("python.exe", "python3", "python"):
        candidate = executable.parent / name
        if candidate.is_file():
            return candidate
    raise AssertionError("FreeCAD GUI MCP environment has no adjacent Python interpreter")


def installed_server_payload(executable: Path) -> dict[str, object]:
    code = r'''import importlib.metadata as m, hashlib, json
d=m.distribution("freecad-mcp")
files={}
for item in d.files or []:
    key=str(item).replace("\\", "/")
    if key.startswith("freecad_mcp/") and key.endswith(".py"):
        path=d.locate_file(item)
        files[key.removeprefix("freecad_mcp/")]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({"version":d.version,"direct_url":json.loads(d.read_text("direct_url.json") or "null"),"files":files},sort_keys=True))'''
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = _run(
        [str(_server_python(executable)), "-I", "-c", code],
        cwd=executable.parent,
        environment=environment,
    )
    output = _require_success(result, "inspect installed FreeCAD GUI MCP")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError("installed FreeCAD GUI MCP provenance is not JSON") from exc
    if not isinstance(payload, dict):
        raise AssertionError("installed FreeCAD GUI MCP provenance is invalid")
    return payload


def _file_url_path(value: object) -> Path:
    if not isinstance(value, str):
        raise AssertionError("installed distribution has no direct source URL")
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise AssertionError("installed distribution is not tied to a local clean checkout")
    try:
        return Path(url2pathname(unquote(parsed.path))).resolve(strict=True)
    except OSError as exc:
        raise AssertionError("installed distribution source checkout does not exist") from exc


def source_path_key(path: Path) -> tuple[str, str]:
    raw = os.fspath(path)
    windows = raw.replace("/", "\\")
    if windows.casefold().startswith("\\\\?\\unc\\"):
        windows = "\\\\" + windows[8:]
    elif windows.casefold().startswith("\\\\?\\"):
        windows = windows[4:]
    if re.match(r"^[A-Za-z]:\\", windows) or windows.startswith("\\\\"):
        return "windows", ntpath.normcase(ntpath.normpath(windows))
    return "native", os.path.normcase(os.path.normpath(raw))


def same_source_path(left: Path, right: Path) -> bool:
    return source_path_key(left) == source_path_key(right)


def source_path_is_within(path: Path, root: Path) -> bool:
    path_kind, path_value = source_path_key(path)
    root_kind, root_value = source_path_key(root)
    if path_kind != root_kind:
        return False
    path_module = ntpath if path_kind == "windows" else os.path
    try:
        common = path_module.commonpath((path_value, root_value))
    except ValueError:
        return False
    return path_module.normcase(common) == path_module.normcase(root_value)


def assert_server_environment_matches_checkout(
    checkout: Path,
    executable: Path,
    evidence: UpstreamEvidence,
    payload: dict[str, object] | None = None,
    *,
    declared_project_version: str = DECLARED_PROJECT_VERSION,
) -> None:
    if os.environ.get("PYTHONPATH", "").strip():
        raise AssertionError("release acceptance rejects repository PYTHONPATH")
    inspected = installed_server_payload(executable) if payload is None else payload
    if inspected.get("version") != declared_project_version:
        raise AssertionError("installed FreeCAD GUI MCP version is not the declared fact")
    direct = inspected.get("direct_url")
    if not isinstance(direct, dict):
        raise AssertionError("installed FreeCAD GUI MCP has no direct_url provenance")
    directory = _file_url_path(direct.get("url"))
    if not same_source_path(directory, checkout):
        raise AssertionError("installed FreeCAD GUI MCP comes from another checkout")
    directory_info = direct.get("dir_info", {})
    if not isinstance(directory_info, dict) or directory_info.get("editable", False):
        raise AssertionError("editable FreeCAD GUI MCP installs are not release evidence")
    if inspected.get("files") != evidence.server_tree:
        raise AssertionError("installed FreeCAD GUI MCP source differs from the checkout")


def assert_matching_addon(
    addon_path: Path,
    evidence: UpstreamEvidence,
    *,
    ignored_names: frozenset[str] | set[str] = frozenset(),
) -> None:
    installed: dict[str, str] = {}
    extras: list[str] = []
    for path in addon_path.rglob("*"):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise AssertionError("installed addon contains a non-regular file")
        relative = path.relative_to(addon_path).as_posix()
        if relative in evidence.addon_tree:
            installed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif (
            "__pycache__" in path.parts
            or path.suffix == ".pyc"
            or path.name in ignored_names
        ):
            continue
        else:
            extras.append(relative)
    if extras:
        raise AssertionError(f"installed addon contains untracked files: {sorted(extras)}")
    if dict(sorted(installed.items())) != evidence.addon_tree:
        raise AssertionError("installed FreeCAD addon differs from the approved checkout")


def load_security_settings(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError("FreeCAD GUI MCP settings are invalid JSON") from exc
    if not isinstance(value, dict):
        raise AssertionError("FreeCAD GUI MCP settings must be a JSON object")
    if value.get("remote_enabled") is not False:
        raise AssertionError("FreeCAD GUI MCP remote_enabled must be false")
    if "allowed_ips" in value and value["allowed_ips"] != "127.0.0.1":
        raise AssertionError("FreeCAD GUI MCP allowed_ips must be loopback-only")
    return value


def assert_path_free_result(result: object, forbidden: set[str]) -> None:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            for token in forbidden:
                if token and token in value:
                    raise AssertionError("live result contains machine-specific identity")

    visit(result)
