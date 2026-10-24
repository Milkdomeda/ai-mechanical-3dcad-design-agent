from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from fea_native_probe import (
    ProbeError,
    assess_native,
    parse_config,
    probe_native,
    select_bundled_tool,
    console_statement,
    verify_tool,
)
from fea_helpers import read_version


def completed_worker() -> dict:
    """Hand-authored boundary fixture, never live compatibility evidence."""
    return {
        "version": "1.1.3",
        "auto_reopen_count": 0,
        "cases": {
            "axial_bar": {
                "displacement_mm": 0.1,
                "stress_MPa": 1.0,
                "reaction_N": [-100.0, 0.0, 0.0],
                "elements": 120,
                "element_nodes": [10],
                "input_element_types": ["C3D10"],
                "finite": True,
                "result_inventory": {"complete": True, "nodes": 300},
                "geometry_valid": True,
                "solid_count": 1,
                "volume_mm3": 10000.0,
                "rf_native_output": True,
                "quality": {"checked_elements": 120, "min_det_mm3": 0.2, "min_scaled_jacobian": 0.3, "thickness_rays": [{"elements": 4, "covered_mm": 10}] * 3},
            },
            "cantilever": {
                "displacement_mm": 0.16,
                "stress_MPa": 6.0,
                "reaction_N": [0.0, 0.0, 10.0],
                "elements": 240,
                "element_nodes": [10],
                "input_element_types": ["C3D10"],
                "finite": True,
                "result_inventory": {"complete": True, "nodes": 600},
                "geometry_valid": True,
                "solid_count": 1,
                "volume_mm3": 20000.0,
                "rf_native_output": True,
                "quality": {"checked_elements": 240, "min_det_mm3": 0.2, "min_scaled_jacobian": 0.3, "thickness_rays": [{"elements": 4, "covered_mm": 10}] * 3},
            },
        },
        "partial_face": {"valid": True, "volume_delta_mm3": 0.0, "solid_count": 1},
        "multiface": {"force_N": [0.0, 0.0, -40.0], "faces": 2, "moment_Nmm": [-200, 2000, 0], "moment_origin_mm": [0, 0, 0], "load_region_valid": True, "loaded_nodes": 100},
        "curved_multiface": {"force_N": [0.0, 0.0, -40.0], "faces": 2, "curved_faces": 2, "moment_Nmm": [-200, 2000, 0], "moment_origin_mm": [0, 0, 0], "load_region_valid": True, "loaded_nodes": 100},
    }


def test_native_probe_records_evidence_and_never_reopens_fem() -> None:
    report = assess_native(completed_worker())
    assert report["schema_version"] == "FEANativeProbe/v1"
    assert report["auto_reopen_count"] == 0
    assert report["automated_status"] == "passed"
    assert report["status"] == "blocked"
    assert report["checks"]["native_gui_roundtrip"]["status"] == "not_executed"
    assert report["checks"]["mesh_quality_calibration"]["status"] == "passed"


@pytest.mark.parametrize("field,value", [("displacement_mm", None), ("stress_MPa", float("nan")), ("elements", 0), ("rf_native_output", False)])
def test_missing_or_invalid_results_cannot_pass(field: str, value: object) -> None:
    worker = completed_worker()
    worker["cases"]["axial_bar"][field] = value
    assert assess_native(worker)["automated_status"] == "failed"


def test_wrong_reaction_or_linear_tets_cannot_pass() -> None:
    worker = completed_worker()
    worker["cases"]["cantilever"]["reaction_N"] = [0, 0, -10]
    worker["cases"]["cantilever"]["element_nodes"] = [4]
    assert assess_native(worker)["automated_status"] == "failed"


def test_native_writer_must_emit_quadratic_tets() -> None:
    worker = completed_worker()
    worker["cases"]["axial_bar"]["input_element_types"] = ["C3D4"]
    assert assess_native(worker)["automated_status"] == "failed"


def test_inverted_or_unresolved_thickness_mesh_cannot_pass() -> None:
    worker = completed_worker()
    worker["cases"]["axial_bar"]["quality"]["min_det_mm3"] = -1
    assert assess_native(worker)["checks"]["mesh_quality_calibration"]["status"] == "failed"


def test_worker_error_cannot_be_hidden_by_valid_measurements() -> None:
    worker = completed_worker()
    worker["error"] = "Failed to finish the probe"
    assert assess_native(worker)["automated_status"] == "failed"


def test_multiface_force_is_one_total_not_per_face() -> None:
    worker = completed_worker()
    worker["multiface"]["force_N"] = [0, 0, -80]
    assert assess_native(worker)["checks"]["multiface_conservation"]["status"] == "failed"


def test_worker_claim_alone_does_not_override_measured_results() -> None:
    worker = completed_worker()
    worker["status"] = "passed"
    worker["auto_reopen_count"] = 1
    assert assess_native(worker)["automated_status"] == "failed"


def test_tool_digest_change_is_rejected(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    tool.write_bytes(b"reviewed executable")
    pin = {"path": str(tool), "sha256": hashlib.sha256(tool.read_bytes()).hexdigest()}
    verify_tool(pin)
    tool.write_bytes(b"replacement")
    with pytest.raises(ProbeError, match="TOOL_DIGEST_MISMATCH"):
        verify_tool(pin)


def test_unknown_cad_or_execution_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"schema_version": "FEAP0Configuration/v1", "source_fcstd": "unknown.FCStd"}))
    with pytest.raises(ProbeError, match="CONFIG"):
        parse_config(path)


def test_probe_invalid_config_does_not_create_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "not-created"
    with pytest.raises(ProbeError):
        probe_native({}, workspace)
    assert not workspace.exists()


def test_config_rejects_duplicate_keys_and_nonfinite(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    for text in ('{"tools":{},"tools":{}}', '{"timeout":NaN}'):
        path.write_text(text)
        with pytest.raises(ProbeError):
            parse_config(path)


def test_result_assessment_does_not_mutate_input() -> None:
    worker = completed_worker()
    before = copy.deepcopy(worker)
    assess_native(worker)
    assert worker == before


def test_ccx_version_only_exit_201_is_not_solver_success() -> None:
    assert read_version("ccx", subprocess.CompletedProcess([], 201, "This is Version 2.23\n", "")) == "2.23"
    with pytest.raises(RuntimeError):
        read_version("gmsh", subprocess.CompletedProcess([], 201, "4.15.0\n", ""))
    with pytest.raises(RuntimeError):
        read_version("ccx", subprocess.CompletedProcess([], 201, "solver failed", ""))


def test_bundle_selects_distribution_bin_not_embedded_environment(tmp_path):
    public = tmp_path / "FreeCAD" / "bin" / "FreeCADCmd.exe"
    embedded = tmp_path / "FreeCAD" / "Library" / "bin" / "FreeCADCmd.exe"
    for path in (public, embedded):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"candidate")
    assert select_bundled_tool(tmp_path, "freecadcmd") == public


def test_two_distribution_bins_are_ambiguous(tmp_path):
    for name in ("First", "Second"):
        path = tmp_path / name / "bin" / "gmsh.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"candidate")
    with pytest.raises(ProbeError, match="AMBIGUOUS"):
        select_bundled_tool(tmp_path, "gmsh")


def test_console_transport_is_ascii_without_losing_unicode_paths(tmp_path):
    import os
    import sys
    script = tmp_path / "中文 空格.py"
    script.write_text("import sys; print(sys.argv[1])", encoding="utf-8")
    statement = console_statement([str(script), "中文 请求"])
    statement.encode("ascii")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-c", statement], capture_output=True, encoding="utf-8", env=env)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "中文 请求"


def test_windows_probe_checkout_preserves_frozen_resource_bytes():
    workflow = (Path(__file__).parents[1] / ".github/workflows/fea-p0.yml").read_text(encoding="utf-8")
    assert workflow.index("git config --global core.autocrlf false") < workflow.index("uses: actions/checkout@")


@pytest.mark.parametrize('kind', ['wrong_moment', 'wrong_region', 'curved_missing', 'inventory_missing'])
def test_force_sum_alone_cannot_pass_distribution_or_field_gate(kind):
    worker = completed_worker()
    if kind == 'wrong_moment': worker['multiface']['moment_Nmm'] = [-100, 2000, 0]
    if kind == 'wrong_region': worker['multiface']['load_region_valid'] = False
    if kind == 'curved_missing': worker.pop('curved_multiface', None)
    if kind == 'inventory_missing': worker['cases']['axial_bar'].pop('result_inventory', None)
    assert assess_native(worker)['automated_status'] == 'failed'


def test_native_cancellation_still_runs_after_save_failure(tmp_path, monkeypatch):
    import fea_native_probe as probe
    axial = tmp_path / 'axial_bar'
    axial.mkdir()
    (axial / 'Mesh.inp').write_text('native generated input')
    calls = []
    monkeypatch.setattr(probe, '_native_cancellation', lambda config, workspace: calls.append(workspace) or {'status': 'passed'})
    report = {'status': 'failed', 'automated_status': 'failed', 'checks': {}}
    probe.record_native_cancellation(report, {}, tmp_path)
    assert calls == [tmp_path]
    assert report['checks']['native_startup_cancellation']['status'] == 'passed'
    assert report['status'] == report['automated_status'] == 'failed'
