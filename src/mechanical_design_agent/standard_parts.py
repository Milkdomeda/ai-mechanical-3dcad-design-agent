from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import StandardPartSettings
from .hashing import file_sha256
from .models import canonical_json
from .package_resources import standard_part_provider_config
from .secure_fs import atomic_publish_new, ensure_managed_directory, read_managed_file
from .standard_part_configuration import load_standard_part_provider_catalog


class StandardPartRegistry:
    """Register validated catalog CAD with deterministic provenance."""

    def __init__(self, settings: StandardPartSettings, repository: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.provider_catalog = load_standard_part_provider_catalog()

    @staticmethod
    def _slug(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._+-]+", "-", value.strip()).strip("-").lower()
        if not normalized:
            raise ValueError("catalog classification must not be empty")
        return normalized

    @staticmethod
    def _part_key(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._+()=\-]+", "-", value.strip()).strip("-.")
        if not normalized or normalized in {".", ".."}:
            raise ValueError("part_number must produce a safe path component")
        return normalized

    def list_providers(self, category: str = "") -> dict[str, object]:
        return self.provider_catalog.as_dict(category)

    def register_download(
        self,
        *,
        provider_id: str,
        file_path: str,
        part_number: str,
        standard: str,
        nominal_size: str,
        source_url: str,
        metadata: dict[str, Any],
        validation_report_path: str,
    ) -> dict[str, object]:
        if self.settings.catalog_root is None:
            raise ValueError("standard-part catalog is not configured")
        provider = next(
            (
                item
                for item in self.provider_catalog.providers
                if item["id"] == provider_id
            ),
            None,
        )
        if provider is None:
            raise ValueError(f"unknown standard-part provider: {provider_id}")
        source = Path(file_path).expanduser().resolve(strict=True)
        if source.suffix.casefold() not in {".step", ".stp", ".fcstd"}:
            raise ValueError("standard part must be STEP, STP, or FCStd")
        if not source.is_relative_to(self.settings.workspace.resolve()):
            raise ValueError("download must first be saved inside the workspace")
        if not all(value.strip() for value in (part_number, standard, nominal_size, source_url)):
            raise ValueError("part number, standard, nominal size, and source URL are required")
        report_path = Path(validation_report_path).expanduser().resolve(strict=True)
        if not report_path.is_relative_to(self.settings.workspace.resolve()):
            raise ValueError("validation report must be inside the workspace")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") != "passed":
            raise ValueError("standard part requires passed validation")
        digest = file_sha256(source)
        manufacturer = self._slug(str(metadata.get("manufacturer") or provider["name"]))
        category = self._slug(str(metadata.get("category") or "uncategorized"))
        target_dir = ensure_managed_directory(
            self.settings.catalog_root
            / self._slug(provider_id)
            / manufacturer
            / category
            / self._part_key(part_number)
            / digest,
            parents=True,
            exist_ok=True,
        ).path
        target = target_dir / source.name
        contents = source.read_bytes()
        if not target.exists():
            atomic_publish_new(target, contents)
        if file_sha256(target) != digest:
            raise RuntimeError("catalog file checksum mismatch")
        manifest = {
            "schema_version": "StandardPartProvenance/v1",
            "provider_id": provider_id,
            "provider_name": provider["name"],
            "trust_tier": provider["trust_tier"],
            "part_number": part_number,
            "standard": standard,
            "nominal_size": nominal_size,
            "source_url": source_url,
            "sha256": digest,
            "file_name": target.name,
            "validation_report": str(report_path),
            "metadata": metadata,
        }
        manifest_path = target_dir / "manifest.json"
        encoded = canonical_json(manifest).encode("utf-8")
        if not manifest_path.exists():
            atomic_publish_new(manifest_path, encoded)
        elif read_managed_file(manifest_path).content != encoded:
            raise ValueError("catalog provenance conflicts with the existing part")
        result: dict[str, object] = {
            "status": "registered",
            "path": str(target),
            "manifest_path": str(manifest_path),
            "sha256": digest,
            "manifest": manifest,
        }
        register = getattr(self.repository, "register_standard_part", None)
        if callable(register):
            result["knowledge_record"] = register(**manifest)
        return result

    def copy_into_design(
        self, *, registered: dict[str, Any], design_root: Path
    ) -> dict[str, object]:
        source = Path(str(registered["path"])).resolve(strict=True)
        expected = str(registered["sha256"])
        read = read_managed_file(source)
        if read.sha256 != expected:
            raise RuntimeError("catalog part changed before design copy")
        target_dir = ensure_managed_directory(
            design_root / "components" / "standard-parts" / expected,
            parents=True,
            exist_ok=True,
        ).path
        target = target_dir / source.name
        if not target.exists():
            atomic_publish_new(target, read.content)
        return {
            "path": str(target),
            "relative_path": target.relative_to(design_root).as_posix(),
            "sha256": expected,
        }


__all__ = ["StandardPartRegistry", "standard_part_provider_config"]
