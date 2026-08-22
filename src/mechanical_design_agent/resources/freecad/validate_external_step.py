"""Run the shared FreeCAD STEP validator with provider-neutral provenance checks.

Usage inside FreeCADCmd:
  freecadcmd validate_external_step.py STEP MANIFEST SPEC REPORT_DIR VALIDATOR_MODULE
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 6:
        raise SystemExit("expected STEP MANIFEST SPEC REPORT_DIR VALIDATOR_MODULE")
    step, manifest_path, specification, report_dir, module_path = map(Path, sys.argv[1:6])
    spec = importlib.util.spec_from_file_location("shared_freecad_model_validation", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load shared FreeCAD validation module")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)

    def provider_neutral_manifest_checks(report, manifest):
        provider = str(manifest.get("provider") or manifest.get("provider_name") or "").strip()
        provider_id = str(manifest.get("provider_id") or "").strip()
        part_id = str(manifest.get("part_id") or manifest.get("part_number") or "").strip()
        source_url = str(manifest.get("source_url") or "").strip()
        designation = str(manifest.get("standard") or "").strip()
        nominal_size = str(manifest.get("nominal_size") or "").strip()
        validator._check(report, "manifest.provider", bool(provider), "Manifest declares the actual provider", actual=provider, expected="non-empty")
        validator._check(report, "manifest.provider-id", bool(provider_id), "Manifest declares a stable provider ID", actual=provider_id, expected="non-empty")
        validator._check(report, "manifest.part-id", bool(part_id), "Manifest declares the provider part ID", actual=part_id, expected="non-empty")
        validator._check(report, "manifest.source", source_url.startswith("https://"), "Manifest declares an HTTPS source URL", actual=source_url, expected="https://...")
        validator._check(report, "manifest.designation", bool(designation), "Manifest declares a standard or catalog designation", actual=designation, expected="non-empty")
        validator._check(report, "manifest.nominal-size", bool(nominal_size), "Manifest declares nominal size", actual=nominal_size, expected="non-empty")

    validator._step_manifest_checks = provider_neutral_manifest_checks
    result = validator.validate_step(step, manifest_path, specification, report_dir)
    print("MECH_EXTERNAL_STEP_VALIDATION=" + json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
