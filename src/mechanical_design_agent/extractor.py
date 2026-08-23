from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .hashing import file_sha256
from .models import finite_vector
from .freecad_runner import run_freecad_script
from .package_resources import freecad_scripts_directory
from .secure_fs import (
    atomic_publish_owned_file,
    ensure_managed_directory,
    remove_owned_tree,
)


class FreeCADExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def extract(self, source: Path, output: Path, timeout_seconds: int = 900) -> dict[str, Any]:
        source = source.expanduser().resolve(strict=True)
        output = Path(os.path.abspath(output.expanduser()))
        if source.suffix.lower() not in {".step", ".stp", ".fcstd"}:
            raise ValueError(f"unsupported CAD source: {source.suffix}")
        before = file_sha256(source)
        ensure_managed_directory(output.parent, parents=True, exist_ok=True)
        attempt = ensure_managed_directory(
            output.parent / f".extract-{uuid.uuid4().hex}",
            parents=False,
            exist_ok=False,
        ).path
        process_output = attempt / "manifest.json"
        try:
            with freecad_scripts_directory() as scripts:
                script_path = scripts / "extract_model_manifest.py"
                if not script_path.is_file():
                    raise FileNotFoundError(script_path)
                completed = run_freecad_script(
                    self.settings.freecadcmd,
                    script_path,
                    [source, process_output],
                    timeout_seconds=timeout_seconds,
                    expected_sha256=self.settings.freecadcmd_sha256,
                    expected_identity=self.settings.freecadcmd_identity,
                    controlled_directory=attempt,
                )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"FreeCAD extraction failed ({completed.returncode}): "
                    f"{completed.stderr[-4000:]}"
                )
            after = file_sha256(source)
            if before != after:
                raise RuntimeError("source CAD file changed during read-only extraction")
            if not process_output.is_file():
                diagnostic = (completed.stderr + "\n" + completed.stdout)[-4000:]
                raise RuntimeError(f"FreeCAD extractor did not create a manifest: {diagnostic}")
            manifest = json.loads(process_output.read_text(encoding="utf-8"))
            self.validate_manifest(manifest, expected_sha256=before)
            atomic_publish_owned_file(
                process_output,
                output,
                replace_existing=True,
            )
            return manifest
        finally:
            remove_owned_tree(
                attempt,
                expected_parent=output.parent,
                label="FreeCAD extraction attempt",
            )

    @staticmethod
    def validate_manifest(manifest: Any, expected_sha256: str = "") -> dict[str, Any]:
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "ModelManifest/v2":
            raise ValueError("invalid ModelManifest/v2")
        source = manifest.get("source")
        if not isinstance(source, dict) or not source.get("sha256"):
            raise ValueError("manifest source identity is missing")
        if expected_sha256 and source["sha256"] != expected_sha256:
            raise ValueError("manifest source hash does not match input file")
        if not isinstance(manifest.get("source_nodes"), list):
            raise ValueError("manifest source_nodes must be an array")
        if not isinstance(manifest.get("geometry_definitions"), list):
            raise ValueError("manifest geometry_definitions must be an array")
        if not isinstance(manifest.get("occurrences"), list):
            raise ValueError("manifest occurrences must be an array")
        if not isinstance(manifest.get("review_view_targets"), list):
            raise ValueError("manifest review_view_targets must be an array")
        finite_vector(list(manifest.get("geometry_vector", [])), 64, "geometry_vector")
        finite_vector(list(manifest.get("structure_vector", [])), 32, "structure_vector")
        return manifest
