from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_freecad_gui_mcp_live_helpers as windows_live  # noqa: E402


def _safe_evidence() -> dict[str, object]:
    return {
        "schema_version": "WindowsFreeCADGuiMcpEvidence/v1",
        "platform": "Windows 11",
        "windows_build": 26200,
        "architecture": "x64",
        "python_version": "3.12.13",
        "freecad_version": "1.1.3",
        "freecad_mcp_commit": "7667e272e1db669ff61dd5411fb4f622691f2dbc",
        "declared_project_version": "0.1.19",
        "committed_lock_version": "0.1.17",
        "evidence_digests": {"LICENSE": "a" * 64},
        "server_tree_digest": "b" * 64,
        "addon_tree_digest": "c" * 64,
        "tool_name_digest": "d" * 64,
        "endpoint": "127.0.0.1:9875",
        "workflow": {
            "initial_dimensions_mm": [40.0, 30.0, 20.0],
            "initial_volume_mm3": 24000.0,
            "final_dimensions_mm": [50.0, 30.0, 20.0],
            "final_volume_mm3": 30000.0,
            "screenshot_present": True,
            "screenshot_bytes": 1024,
            "screenshot_sha256": "e" * 64,
            "manifest_schema": "ModelManifest/v2",
            "shape_count": 1,
            "geometry_count": 1,
            "source_hash_unchanged": True,
        },
        "cleanup": {
            "synthetic_document_closed": True,
            "document_set_restored": True,
            "stdio_child_stopped": True,
            "attempt_files_removed": True,
            "upstream_unchanged": True,
            "addon_unchanged": True,
            "settings_unchanged": True,
            "repository_unchanged": True,
        },
    }


def test_w4_evidence_accepts_only_the_versioned_safe_schema() -> None:
    evidence = _safe_evidence()
    assert windows_live.validate_safe_evidence(evidence) == evidence


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "WindowsFreeCADGuiMcpEvidence/v2", "schema"),
        ("freecad_version", "1.1.1", "FreeCAD"),
        ("endpoint", "0.0.0.0:9875", "loopback"),
        ("checkout_path", r"C:\\Users\\person\\freecad-mcp", "field"),
        ("database_url", "postgresql://secret@localhost/db", "field"),
    ],
)
def test_w4_evidence_rejects_wrong_boundary_and_forbidden_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    evidence = _safe_evidence()
    evidence[field] = value
    with pytest.raises(AssertionError, match=message):
        windows_live.validate_safe_evidence(evidence)


@pytest.mark.parametrize(
    "secret",
    [
        r"C:\\Users\\person\\checkout",
        r"\\server\\share\\addon",
        "/" + "Users/person/checkout",
        "bolt://neo4j:password@127.0.0.1:7687",
        "internal-project-identifier",
        "192.168.1.10:9875",
    ],
)
def test_w4_evidence_deep_privacy_scan_rejects_sensitive_strings(secret: str) -> None:
    evidence = _safe_evidence()
    evidence["workflow"]["unexpected"] = secret  # type: ignore[index]
    with pytest.raises(AssertionError, match="safe evidence"):
        windows_live.validate_safe_evidence(evidence)


def test_cleanup_failure_overrides_a_passing_workflow() -> None:
    evidence = _safe_evidence()
    for key in tuple(evidence["cleanup"]):  # type: ignore[arg-type]
        failed = copy.deepcopy(evidence)
        failed["cleanup"][key] = False  # type: ignore[index]
        with pytest.raises(AssertionError, match="cleanup"):
            windows_live.validate_safe_evidence(failed)


def test_evidence_never_records_preexisting_document_names() -> None:
    evidence = _safe_evidence()
    evidence["initial_documents"] = ["private-model"]
    with pytest.raises(AssertionError, match="field"):
        windows_live.validate_safe_evidence(evidence)
