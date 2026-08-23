from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import Settings
from .hashing import file_sha256
from .standard_part_configuration import (
    load_standard_part_provider_catalog,
    load_standard_part_sources,
)
from .workspace_bootstrap import BootstrapFailure, read_workspace_manifest
from .secure_fs import atomic_publish_new, ensure_managed_directory, read_managed_file


class StandardPartRegistry:
    def __init__(self, settings: Settings, repository: Any):
        self.settings = settings
        self.repository = repository
        self.provider_catalog = load_standard_part_provider_catalog()
        manifest = read_workspace_manifest(settings.workspace)
        self.sources = load_standard_part_sources(manifest)
        self.providers = self.provider_catalog.providers
        self.catalog_root = self.sources.effective_root

    @staticmethod
    def _slug(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._+-]+", "-", value.strip()).strip("-").lower()
        if not normalized:
            raise ValueError("catalog classification values must not be empty")
        return normalized

    @staticmethod
    def _part_key(value: str) -> str:
        """Keep manufacturer part-number spelling while making one safe path segment."""
        normalized = re.sub(r"[^A-Za-z0-9._+()=\-]+", "-", value.strip()).strip("-.")
        if not normalized or normalized in {".", ".."}:
            raise ValueError("part_number must produce a safe catalog path segment")
        return normalized

    def list_providers(self, category: str = "") -> dict[str, Any]:
        return self.provider_catalog.as_dict(category)

    def _require_catalog_root(self) -> Path:
        if self.catalog_root is None:
            raise BootstrapFailure(
                "STANDARD_PART_CATALOG_DISABLED",
                "configure an existing standard-part catalog before registration",
                status="setup_required",
            )
        return self.catalog_root

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
        approval_reference: str,
        validation_report_path: str,
    ) -> dict[str, Any]:
        catalog_root = self._require_catalog_root()
        provider = next((item for item in self.providers if item["id"] == provider_id), None)
        if provider is None:
            raise ValueError(f"unknown standard-part provider: {provider_id}")
        source = Path(file_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in {".step", ".stp", ".fcstd"}:
            raise ValueError("standard part must be STEP/STP/FCStd")
        if not source.is_relative_to(self.settings.workspace):
            raise ValueError("standard-part download must first be saved inside the workspace")
        if not all(value.strip() for value in (part_number, standard, nominal_size, source_url)):
            raise ValueError("part_number, standard, nominal_size, and source_url are required")
        if not approval_reference.strip():
            raise ValueError("engineer approval_reference is required before adding a reusable local standard part")
        report_path = Path(validation_report_path).expanduser().resolve(strict=True)
        if not report_path.is_file() or not report_path.is_relative_to(self.settings.workspace):
            raise ValueError("validation report must be a workspace JSON artifact")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") != "passed":
            raise ValueError("only a passed validation report can authorize reusable catalog storage")
        digest = file_sha256(source)
        category = self._slug(str(metadata.get("category") or "uncategorized"))
        if provider_id == "step-parts":
            target_dir = catalog_root / "step-parts" / self._part_key(part_number) / digest
        else:
            manufacturer = self._slug(str(metadata.get("manufacturer") or provider["name"]))
            target_dir = catalog_root / self._slug(provider_id) / manufacturer / category / self._part_key(part_number) / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if not target.exists():
            shutil.move(str(source), str(target))
            source_disposition = "moved_to_approved_catalog"
        else:
            source_disposition = "catalog_duplicate_verified_source_retained"
        if file_sha256(target) != digest:
            raise RuntimeError("approved catalog file checksum mismatch")
        registered_metadata = {
            **metadata,
            "approval_status": "approved_for_reuse",
            "approval_reference": approval_reference.strip(),
            "validation_report": str(report_path),
            "source_disposition": source_disposition,
        }
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
            "metadata": registered_metadata,
            "approval_status": "approved_for_reuse",
            "approval_reference": approval_reference.strip(),
            "validation_report": str(report_path),
            "source_disposition": source_disposition,
        }
        manifest_path = target_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = self.repository.register_standard_part(
            provider_id=provider_id,
            provider_name=provider["name"],
            trust_tier=provider["trust_tier"],
            part_number=part_number,
            standard=standard,
            nominal_size=nominal_size,
            source_url=source_url,
            sha256=digest,
            local_path=str(target),
            manifest_path=str(manifest_path),
            metadata=registered_metadata,
            approval_reference=approval_reference.strip(),
            validation_report_path=str(report_path),
        )
        return {**record, "manifest": manifest}

    def copy_into_job(
        self,
        *,
        registered: dict[str, Any],
        job_root: Path,
        working_copy_id: str,
    ) -> dict[str, Any]:
        """Retain one actually selected catalog part and provenance inside its Job."""
        source_value = registered.get("local_path") or registered.get("manifest", {}).get("local_path")
        if not isinstance(source_value, str) or not source_value:
            raise ValueError("registered standard part has no controlled catalog path")
        source = Path(source_value)
        evidence = read_managed_file(source)
        manifest = dict(registered.get("manifest") or {})
        expected = manifest.get("sha256") or registered.get("sha256")
        if expected != evidence.sha256:
            raise RuntimeError("registered standard-part checksum changed before Job copy")
        part_id = self._part_key(str(manifest.get("part_number") or registered.get("part_number") or evidence.sha256))
        target_dir = ensure_managed_directory(
            job_root / "components" / "standard-parts" / part_id / evidence.sha256,
            parents=True,
            exist_ok=True,
        ).path
        target = target_dir / source.name
        provenance = target_dir / "manifest.json"
        if not target.exists():
            atomic_publish_new(target, evidence.content)
        provenance_payload = {
            **manifest,
            "schema_version": "JobStandardPartProvenance/v1",
            "working_copy_id": working_copy_id,
            "job_relative_path": target.relative_to(job_root).as_posix(),
        }
        encoded = (
            json.dumps(provenance_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if not provenance.exists():
            atomic_publish_new(provenance, encoded)
        return {
            "path": str(target),
            "relative_path": target.relative_to(job_root).as_posix(),
            "sha256": evidence.sha256,
            "provenance_path": str(provenance),
        }
