from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Settings
from .extractor import FreeCADExtractor
from .hashing import file_sha256
from .learning import generate_question_targets
from .secure_fs import atomic_publish_new, atomic_replace, ensure_managed_directory


def run_test_fixture(settings: Settings, source_path: str) -> dict[str, Any]:
    """Extract a local fixture without registering it as product-family knowledge."""
    source = Path(source_path).expanduser().resolve(strict=True)
    fixture_root = settings.package_root / "data" / "test-fixture"
    manifest_path = fixture_root / "fixture-manifest.json"
    report_path = fixture_root / "smoke-report.json"
    source_sha256_before = file_sha256(source)
    manifest = FreeCADExtractor(settings).extract(source, manifest_path)
    source_sha256_after = file_sha256(source)
    if source_sha256_after != source_sha256_before:
        raise RuntimeError("test fixture source changed during read-only extraction")
    targets = generate_question_targets(manifest, family_confirmed=False, limit=5)
    report = {
        "schema_version": "TestFixtureSmoke/v1",
        "scope": "test-fixture",
        "formal_product_family_membership": False,
        "knowledge_promotion_allowed": False,
        "source_sha256": source_sha256_after,
        "source_sha256_before": source_sha256_before,
        "source_sha256_after": source_sha256_after,
        "source_hash_unchanged": True,
        "parser_version": manifest["parser_version"],
        "counts": {
            "source_nodes": len(manifest["source_nodes"]),
            "geometry_definitions": len(manifest["geometry_definitions"]),
            "occurrences": len(manifest["occurrences"]),
            "relation_candidates": len(manifest["relation_candidates"]),
            "review_view_targets": len(manifest["review_view_targets"]),
            "diagnostics": len(manifest["diagnostics"]),
        },
        "first_question_batch": targets,
        "manifest_path": str(manifest_path),
    }
    fixture_root = ensure_managed_directory(
        fixture_root,
        parents=True,
        exist_ok=True,
    ).path
    report_bytes = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_publish_new(report_path, report_bytes)
    except FileExistsError:
        atomic_replace(report_path, report_bytes)
    report["report_path"] = str(report_path)
    return report
