"""Optional, read-only FEA runtime configuration; CAD readiness is independent."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .analysis_protocol import parse_record
from .analysis_types import AnalysisError, JsonObject
from .bootstrap_diagnostics import DiagnosticGateError
from .models import require_safe_id
from .secure_fs import SecureFilesystemError, read_managed_file, validate_managed_path

_R1_MODULE_SHA256 = "ca4af36e48f0b1cb6b3b57e7c63e61ed7a3de768df497d50d4fa171493634c53"
_FREECAD_COMMIT = "145529fe741292ff0b3977a01195bf0247425794"


def _resolve(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace / path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError("FEA_RUNTIME_INVALID", "duplicate runtime manifest field")
        result[key] = value
    return result


def _read_pinned(path):
    managed=validate_managed_path(path,allow_missing_leaf=False)
    content=read_managed_file(managed.path)
    if content.identity!=managed.identity:
        raise AnalysisError('FEA_RUNTIME_CHANGED','file identity changed while reading FEA configuration')
    return managed.path,content


def _verify_manifest(config: JsonObject, workspace: Path, *, checkpoint=None) -> dict:
    reference = config["runtime"]
    try:
        path = _resolve(workspace, reference["manifest_path"])
        manifest_path,pinned = _read_pinned(path)
        if pinned.sha256 != reference["manifest_sha256"]:
            raise AnalysisError("FEA_RUNTIME_CHANGED", "FEA runtime manifest checksum changed")
        manifest = json.loads(pinned.content.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if manifest["schema_version"] != "FEAR1Runtime/v1" or manifest["profile"] != reference["profile"] or manifest["source_commit"] != _FREECAD_COMMIT or manifest["source_unchanged"] is not True:
            raise AnalysisError("FEA_RUNTIME_UNSUPPORTED", "runtime is not the reviewed FreeCAD R1 profile")
        if manifest["native_identity"]["app_version"][:3] != ["1", "1", "3"]:
            raise AnalysisError("FEA_RUNTIME_UNSUPPORTED", "FreeCAD 1.1.3 is required")
        root = validate_managed_path(_resolve(workspace, manifest["runtime_root"]), allow_missing_leaf=False).path
        module_path,module = _read_pinned(_resolve(workspace, manifest["module_path"]))
        if not module_path.is_relative_to(root) or module.sha256 != _R1_MODULE_SHA256 or manifest["module_sha256"] != _R1_MODULE_SHA256:
            raise AnalysisError("FEA_RUNTIME_CHANGED", "reviewed native FEM source changed")
        for name, tool in config["tools"].items():
            tool_path,actual = _read_pinned(_resolve(workspace, tool["path"]))
            if not tool_path.is_relative_to(root):
                raise AnalysisError("FEA_RUNTIME_INVALID", "analysis executables must belong to the isolated runtime")
            if name != "freecadgui" and manifest["tools"][name] != tool:
                raise AnalysisError("FEA_RUNTIME_CHANGED", "runtime and configured tools disagree")
        expected = manifest["tree"]
        actual = {}
        for child in sorted(root.rglob("*")):
            if checkpoint is not None: checkpoint()
            relative = child.relative_to(root).as_posix()
            if getattr(child, "is_junction", lambda: False)():
                raise AnalysisError("FEA_RUNTIME_CHANGED", "runtime junction is unsupported")
            if child.is_symlink():
                if not child.resolve(strict=True).is_relative_to(root):
                    raise AnalysisError("FEA_RUNTIME_CHANGED", "runtime symlink escapes the isolated copy")
                actual[relative] = {"symlink": os.readlink(child)}
            elif child.is_file():
                facts = read_managed_file(child)
                actual[relative] = {"sha256": facts.sha256}
        if actual != expected:
            raise AnalysisError("FEA_RUNTIME_CHANGED", "isolated runtime inventory or checksum changed")
        return {"manifest_path": str(manifest_path), "manifest_sha256": pinned.sha256, "module_sha256": module.sha256, "native_identity": manifest["native_identity"]}
    except AnalysisError:
        raise
    except (OSError, SecureFilesystemError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise AnalysisError("FEA_RUNTIME_INVALID", "FEA runtime manifest or files cannot be verified") from exc


def load_analysis_configuration(runtime, *, checkpoint=None) -> JsonObject:
    if checkpoint is not None: checkpoint()
    manifest = runtime._manifest()
    workspace = manifest.workspace
    value = str(runtime.environ.get("MECH_DESIGN_FEA_CONFIG", "")).strip()
    selected = _resolve(workspace, value) if value else workspace / "config" / "fea.json"
    try:
        selected,pinned = _read_pinned(selected)
    except FileNotFoundError as exc:
        raise AnalysisError("FEA_CONFIG_MISSING", "run the explicit FEA setup command for this workspace") from exc
    except (OSError, SecureFilesystemError) as exc:
        if not selected.exists():
            raise AnalysisError("FEA_CONFIG_MISSING", "run the explicit FEA setup command for this workspace") from exc
        raise AnalysisError("FEA_CONFIG_INVALID", "FEA configuration is not a safe regular file") from exc
    try:
        config = parse_record(pinned.content.decode("utf-8"), schema_name="configuration")
    except UnicodeError as exc:
        raise AnalysisError('FEA_CONFIG_INVALID','FEA configuration must use UTF-8') from exc
    tools = {}
    for name, tool in config["tools"].items():
        if checkpoint is not None: checkpoint()
        supported = {"1.1.3"} if name.startswith("freecad") else ({"4.15.0"} if name == "gmsh" else {"2.22", "2.23"})
        if tool["version"] not in supported:
            raise AnalysisError("FEA_TOOL_VERSION_UNSUPPORTED", "configured native version requires a compatibility run", details={"tool": name})
        try:
            tool_path,actual = _read_pinned(_resolve(workspace, tool["path"]))
        except (OSError, SecureFilesystemError) as exc:
            raise AnalysisError("FEA_TOOL_UNAVAILABLE", "configured native executable cannot be pinned", details={"tool": name}) from exc
        if actual.sha256 != tool["sha256"] or actual.link_count != 1:
            raise AnalysisError("FEA_TOOL_DIGEST_CHANGED", "configured native executable identity or checksum changed", details={"tool": name})
        tools[name] = {**tool, "path": str(tool_path), "identity": {"volume": actual.identity.volume, "file_index": actual.identity.file_index}}
    proof = _verify_manifest(config, workspace, checkpoint=checkpoint)
    try:
        scratch = validate_managed_path(_resolve(workspace, config["native_scratch_root"]), allow_missing_leaf=True).path
    except (OSError,SecureFilesystemError) as exc:
        raise AnalysisError('FEA_SCRATCH_INVALID','configured native scratch root cannot be pinned') from exc
    if not str(scratch).isascii():
        raise AnalysisError("FEA_SCRATCH_ASCII_REQUIRED", "configure a private ASCII native scratch root for W1")
    return {"schema_version": "AnalysisSettings/v1", "workspace": str(workspace), "configuration_path": str(selected), "configuration_sha256": pinned.sha256, "configuration": config, "tools": tools, "runtime": proof, "native_scratch_root": str(scratch), "verification_scope": "configured file identities; no native process or GUI probe executed"}


def analysis_capabilities(runtime, *, design_id: str | None = None) -> JsonObject:
    if design_id is not None:
        require_safe_id(design_id, "design_id")
    result = {"schema_version": "AnalysisCapabilities/v1", "analysis_type": "linear_static", "conclusion_levels": ["screening_only", "criteria_evaluated"], "engineering_certification": False}
    try:
        settings = load_analysis_configuration(runtime)
        return {**result, "status": "runtime_configured", "code": "FEA_RUNTIME_FILES_VERIFIED", "tools": settings["tools"], "runtime": settings["runtime"], "verification_scope": settings["verification_scope"], "gui_status": "not_probed", "numerical_acceptance": "not_executed"}
    except AnalysisError as exc:
        return {**result, **exc.as_dict(), "status": "setup_required", "next_action": "Configure and verify the isolated FEA runtime; CAD remains independent."}
    except DiagnosticGateError as exc:
        components=exc.response.get('diagnostics',{}).get('components',[])
        workspace_code=next((c['code'] for c in components if c.get('name')=='workspace'),exc.response['code'])
        return {**result,'status':'setup_required','code':workspace_code,'message':str(exc),'next_action':'Initialize the mechanical design workspace first.'}
