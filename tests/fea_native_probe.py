"""P0-only native compatibility harness; never imported by product code.

The child builds synthetic geometry. No request accepts a user CAD file and
no saved FEM document is reopened. Incomplete gates stay explicitly blocked.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import time


class ProbeError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("CONFIG_DUPLICATE_KEY")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ProbeError(f"CONFIG_NONFINITE: {value}")


def parse_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError("CONFIG_INVALID_JSON") from exc
    _validate_config(value)
    return value


def _validate_config(value):
    if not isinstance(value, dict) or set(value) - {"schema_version", "tools", "timeout_seconds"}:
        raise ProbeError("CONFIG_UNKNOWN_FIELD")
    if value.get("schema_version") != "FEAP0Configuration/v1":
        raise ProbeError("CONFIG_VERSION")
    tools = value.get("tools")
    if not isinstance(tools, dict) or set(tools) != {"freecadcmd", "gmsh", "ccx"}:
        raise ProbeError("CONFIG_TOOLS")
    for name, pin in tools.items():
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256", "version"}:
            raise ProbeError(f"CONFIG_TOOL: {name}")
        if not isinstance(pin["path"], str) or not Path(pin["path"]).is_absolute():
            raise ProbeError("CONFIG_ABSOLUTE_TOOL_REQUIRED")
        if not re.fullmatch("[0-9a-f]{64}", str(pin["sha256"])) or not isinstance(pin["version"], str) or not pin["version"]:
            raise ProbeError("CONFIG_TOOL_IDENTITY")
    if tools["freecadcmd"]["version"] != "1.1.3":
        raise ProbeError("CONFIG_FREECAD_VERSION")
    timeout = value.get("timeout_seconds", 300)
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ProbeError("CONFIG_TIMEOUT")


def verify_tool(pin: dict) -> dict:
    path = Path(pin["path"])
    if path.is_symlink() or not path.is_file():
        raise ProbeError("TOOL_NOT_REGULAR")
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ProbeError("TOOL_CHANGED_DURING_READ")
    if digest != pin["sha256"]:
        raise ProbeError("TOOL_DIGEST_MISMATCH")
    return {"sha256": digest, "size_bytes": after.st_size, "device": after.st_dev, "file_index": after.st_ino}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _near(value, expected, rtol):
    return _number(value) and abs(value - expected) <= max(abs(expected) * rtol, 1e-9)


def assess_native(worker: dict) -> dict:
    checks = {}

    def check(name, passed, details=None):
        checks[name] = {"status": "passed" if passed else "failed", "details": details}

    check("freecad_version", worker.get("version") == "1.1.3")
    check("worker_completed", "error" not in worker)
    check("no_automatic_fem_reopen", worker.get("auto_reopen_count") == 0)
    for name, displacement, stress, force, rtol, volume in (
        ("axial_bar", 0.1, 1.0, [-100, 0, 0], 0.02, 10000),
        ("cantilever", 0.16, 6.0, [0, 0, 10], 0.05, 20000),
    ):
        result = worker.get("cases", {}).get(name, {})
        reaction = result.get("reaction_N")
        check(name + "_displacement", _near(result.get("displacement_mm"), displacement, rtol), result.get("displacement_mm"))
        check(name + "_stress", _near(result.get("stress_MPa"), stress, rtol), result.get("stress_MPa"))
        check(name + "_reaction", isinstance(reaction, list) and len(reaction) == 3 and all(_near(a, b, 0.001) for a, b in zip(reaction, force)), reaction)
        check(name + "_native_rf", result.get("rf_native_output") is True)
        check(name + "_mesh", type(result.get("elements")) is int and result["elements"] > 0 and result.get("element_nodes") == [10] and result.get("input_element_types") == ["C3D10"] and result.get("finite") is True)
        check(name + "_geometry", result.get("geometry_valid") is True and result.get("solid_count") == 1 and _near(result.get("volume_mm3"), volume, 1e-9))
    qualities = [worker.get("cases", {}).get(name, {}).get("quality", {}) for name in ("axial_bar", "cantilever")]
    quality_ok = all(_number(q.get("min_det_mm3")) and q["min_det_mm3"] > 0 and _number(q.get("min_scaled_jacobian")) and q["min_scaled_jacobian"] > 0 and q.get("checked_elements") == worker["cases"][name].get("elements") and len(q.get("thickness_rays", [])) == 3 and all(ray.get("elements", 0) >= 2 and _near(ray.get("covered_mm"), 10, 1e-6) for ray in q["thickness_rays"]) for name, q in zip(("axial_bar", "cantilever"), qualities))
    check("mesh_quality_calibration", quality_ok, {"scope": "straight synthetic benchmarks", "criterion": "15-point sampled determinant/scaled Jacobian > 0 and >=2 elements on each of 3 full-thickness rays", "measurements": qualities})
    patch = worker.get("partial_face", {})
    check("partial_face", patch.get("valid") is True and patch.get("solid_count") == 1 and _near(patch.get("volume_delta_mm3"), 0, 0))
    force = worker.get("multiface", {}).get("force_N")
    check("multiface_conservation", isinstance(force, list) and len(force) == 3 and all(_near(a, b, 1e-9) for a, b in zip(force, [0, 0, -40])) and worker.get("multiface", {}).get("faces", 0) >= 2, force)
    automated = "passed" if all(item["status"] == "passed" for item in checks.values()) else "failed"
    for name in ("native_gui_roundtrip",):
        checks[name] = {"status": "not_executed", "details": "Separate same-version evidence required; not inferred from solver success."}
    return {
        "schema_version": "FEANativeProbe/v1",
        "platform": platform.system(),
        "automated_status": automated,
        "status": "blocked" if automated == "passed" else "failed",
        "auto_reopen_count": worker.get("auto_reopen_count"),
        "checks": checks,
        "worker": worker,
    }


def probe_native(config: dict, workspace: Path) -> dict:
    _validate_config(config)
    identities = {name: verify_tool(pin) for name, pin in config["tools"].items()}
    workspace = Path(workspace).absolute()
    if workspace.exists() or any(parent.is_symlink() for parent in (workspace, *workspace.parents)):
        raise ProbeError("WORKSPACE_MUST_BE_NEW_AND_LOCAL")
    repository = Path(__file__).resolve().parents[1]
    if workspace.is_relative_to(repository):
        raise ProbeError("WORKSPACE_MUST_BE_OUTSIDE_SOURCE")
    workspace.mkdir(parents=True)
    request = workspace / "request.json"
    request.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    worker = Path(__file__).with_name("fea_helpers.py")
    response = workspace / "native-result.json"
    args = [str(worker), str(request), str(response)]
    statement = "import sys, runpy; sys.argv=" + repr(args) + "; runpy.run_path(sys.argv[0], run_name='__main__')"
    env = {k: v for k, v in os.environ.items() if k in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    env.update({"HOME": str(workspace), "USERPROFILE": str(workspace), "TMPDIR": str(workspace), "TMP": str(workspace), "TEMP": str(workspace), "PATH": os.defpath, "LANG": "C.UTF-8", "OMP_NUM_THREADS": "1", "QT_QPA_PLATFORM": "offscreen"})
    command = [config["tools"]["freecadcmd"]["path"], "-u", str(workspace / "user.cfg"), "-s", str(workspace / "system.cfg"), "-c", statement]
    from mechanical_design_agent.analysis_process import ProcessController, ProcessRequest
    controller = ProcessController()
    identity = controller.start(ProcessRequest(argv=tuple(command), cwd=workspace, env=env, stdout_path=workspace / "freecad.stdout.log", stderr_path=workspace / "freecad.stderr.log", timeout_seconds=config.get("timeout_seconds", 300)))
    try:
        while (completed := controller.poll(identity)) is None:
            time.sleep(0.05)
    finally:
        controller.terminate(identity, reason="interrupted")
    for name, pin in config["tools"].items():
        if verify_tool(pin) != identities[name]:
            raise ProbeError("TOOL_IDENTITY_CHANGED")
    if completed.returncode or completed.reason != "exited" or not response.is_file():
        raise ProbeError(f"NATIVE_PROBE_FAILED: reason={completed.reason}, returncode={completed.returncode}; see isolated logs")
    result = json.loads(response.read_text(encoding="utf-8"), parse_constant=_invalid_constant)
    if "error" not in result and any(result.get("versions", {}).get(name) != pin["version"] for name, pin in config["tools"].items()):
        raise ProbeError("TOOL_VERSION_MISMATCH")
    report = assess_native(result)
    report["tool_identities"] = identities
    report["artifacts"] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                report["artifacts"][path.relative_to(workspace).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    (workspace / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report
