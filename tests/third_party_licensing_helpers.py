from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import tomllib
from typing import Any
from urllib.parse import urlparse

from packaging.utils import canonicalize_name


SCHEMA_VERSION = "ThirdPartyComponents/v1"
PROJECT_DISTRIBUTION = "ai-mechanical-3dcad-design-agent"
PROJECT_VERSION = "0.4.0"
TOP_LEVEL_FIELDS = {
    "schema_version",
    "project_distribution",
    "project_version",
    "audit_date",
    "lockfile",
    "provider_config",
    "local_provider_ids",
    "components",
}
COMPONENT_FIELDS = {
    "id",
    "name",
    "component_kind",
    "scope",
    "relationship",
    "distribution",
    "direct",
    "version",
    "tag",
    "commit",
    "image",
    "image_digest",
    "identity_not_applicable_reason",
    "spdx",
    "attribution",
    "source_url",
    "license_url",
    "purpose",
    "evidence_source",
    "evidence_sha256",
    "audit_date",
    "validation_state",
    "provider_ids",
    "notes",
    "declared_project_version",
    "committed_lock_version",
}
REQUIRED_COMPONENT_FIELDS = {
    "id",
    "name",
    "component_kind",
    "scope",
    "relationship",
    "distribution",
    "spdx",
    "attribution",
    "source_url",
    "license_url",
    "purpose",
    "evidence_source",
    "audit_date",
    "validation_state",
}
SCOPES = {"runtime", "build", "test", "integration"}
RELATIONSHIPS = {
    "installed_dependency",
    "external_integration",
    "external_service",
    "catalog_web_provider",
}
DISTRIBUTIONS = {"not_distributed_by_project"}
VALIDATION_STATES = {"verified"}
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ThirdPartyInventory:
    schema_version: str
    project_distribution: str
    project_version: str
    audit_date: date
    lockfile: str
    provider_config: str
    local_provider_ids: tuple[str, ...]
    components: tuple[dict[str, object], ...]


def _require_nonblank(record: dict[str, Any], fields: set[str], label: str) -> None:
    missing = fields - record.keys()
    if missing:
        raise ValueError(f"{label} missing required fields: {sorted(missing)}")
    for field in fields:
        value = record[field]
        if isinstance(value, str) and not value.strip():
            raise ValueError(f"{label}.{field} must not be blank")


def _require_https(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be an HTTPS URL")


def _validate_component(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"components[{index}] must be a table")
    record = dict(raw)
    unknown = record.keys() - COMPONENT_FIELDS
    if unknown:
        raise ValueError(f"components[{index}] has unknown fields: {sorted(unknown)}")
    label = f"components[{index}]"
    _require_nonblank(record, REQUIRED_COMPONENT_FIELDS, label)
    if record["scope"] not in SCOPES:
        raise ValueError(f"{label}.scope is invalid")
    if record["relationship"] not in RELATIONSHIPS:
        raise ValueError(f"{label}.relationship is invalid")
    if record["distribution"] not in DISTRIBUTIONS:
        raise ValueError(f"{label}.distribution is invalid")
    if record["validation_state"] not in VALIDATION_STATES:
        raise ValueError(f"{label}.validation_state is invalid")
    if not isinstance(record["audit_date"], date):
        raise ValueError(f"{label}.audit_date must be a TOML date")
    for field in ("source_url", "license_url", "evidence_source"):
        _require_https(record[field], f"{label}.{field}")

    identities = [
        field
        for field in ("version", "commit", "image", "identity_not_applicable_reason")
        if isinstance(record.get(field), str) and str(record[field]).strip()
    ]
    if not identities:
        raise ValueError(f"{label} requires an exact identity or reason")
    if "identity_not_applicable_reason" in identities and len(identities) != 1:
        raise ValueError(f"{label} mixes an exact identity with not-applicable")
    if "commit" in record and not COMMIT.fullmatch(str(record["commit"])):
        raise ValueError(f"{label}.commit must be a full lowercase Git commit")

    image = record.get("image")
    image_digest = record.get("image_digest")
    if isinstance(image, str) and image.strip():
        if record["relationship"] != "external_service":
            raise ValueError(f"{label}.image is only valid for external services")
        if not isinstance(image_digest, str) or not image_digest.strip():
            raise ValueError(f"{label} requires image_digest")
        if not IMAGE_SHA256.fullmatch(image_digest):
            raise ValueError(f"{label}.image_digest must be a lowercase image SHA-256")
    elif image_digest is not None:
        raise ValueError(
            f"{label}.image_digest is only valid for image-backed external services"
        )

    evidence_sha256 = record.get("evidence_sha256")
    immutable_evidence = any(
        token in str(record["evidence_source"])
        for token in (str(record.get("commit", "")), str(record.get("image", "")))
        if token
    )
    if evidence_sha256 is not None:
        if not isinstance(evidence_sha256, str) or not SHA256.fullmatch(evidence_sha256):
            raise ValueError(f"{label}.evidence_sha256 must be lowercase SHA-256")
    elif not immutable_evidence:
        raise ValueError(f"{label} requires evidence_sha256 or immutable evidence")

    installed = record["relationship"] == "installed_dependency"
    if installed:
        if not isinstance(record.get("direct"), bool):
            raise ValueError(f"{label}.direct must be Boolean")
        if record["scope"] not in {"runtime", "build", "test"}:
            raise ValueError(f"{label} installed dependency has invalid scope")
    elif "direct" in record:
        raise ValueError(f"{label}.direct is only valid for installed dependencies")

    provider_ids = record.get("provider_ids", [])
    if not isinstance(provider_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in provider_ids
    ):
        raise ValueError(f"{label}.provider_ids must be nonblank strings")

    if record["spdx"] == "NOASSERTION":
        if record["relationship"] != "catalog_web_provider":
            raise ValueError(f"{label} NOASSERTION is catalog-only")
        notes = str(record.get("notes", "")).lower()
        if "service terms" not in notes or "not an open-source software license" not in notes:
            raise ValueError(f"{label} must state the service-terms boundary")

    if record["id"] == "freecad-mcp":
        if record.get("commit") != "7667e272e1db669ff61dd5411fb4f622691f2dbc":
            raise ValueError("freecad-mcp must retain the approved commit")
        if record.get("tag") != "none":
            raise ValueError("freecad-mcp must record that no tag was approved")
        if record.get("declared_project_version") != "0.1.19":
            raise ValueError("freecad-mcp declared project version must remain 0.1.19")
        if record.get("committed_lock_version") != "0.1.17":
            raise ValueError("freecad-mcp committed lock version must remain 0.1.17")
        if "version" in record:
            raise ValueError("freecad-mcp must not normalize its two upstream versions")

    expected_workbench_licenses = {
        "freecad-fasteners-workbench": "GPL-2.0-or-later",
        "freecad-gears-workbench": "GPL-3.0-or-later",
    }
    expected_license = expected_workbench_licenses.get(str(record["id"]))
    if expected_license and record["spdx"] != expected_license:
        raise ValueError(f"{label} must retain {expected_license}")
    return record


def load_third_party_inventory(root: Path) -> ThirdPartyInventory:
    path = root / "third-party-components.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = raw.keys() - TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(f"inventory has unknown fields: {sorted(unknown)}")
    required = TOP_LEVEL_FIELDS
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"inventory missing fields: {sorted(missing)}")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported third-party inventory schema")
    if raw["project_distribution"] != PROJECT_DISTRIBUTION:
        raise ValueError("unexpected project distribution")
    if raw["project_version"] != PROJECT_VERSION:
        raise ValueError("unexpected project version")
    if not isinstance(raw["audit_date"], date):
        raise ValueError("inventory audit_date must be a TOML date")
    if raw["lockfile"] != "uv.lock":
        raise ValueError("inventory must be checked against uv.lock")
    if raw["provider_config"] != (
        "src/mechanical_design_agent/resources/config/standard_part_providers.json"
    ):
        raise ValueError("inventory provider config path is invalid")
    local_ids = raw["local_provider_ids"]
    if local_ids != ["verified-local"]:
        raise ValueError("verified-local must be the sole local provider classification")
    components = tuple(
        _validate_component(component, index)
        for index, component in enumerate(raw["components"])
    )
    ids = [str(component["id"]) for component in components]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate component ids")
    identities = [
        (
            str(component["name"]),
            str(component.get("version", "")),
            str(component.get("commit", "")),
            str(component.get("image", "")),
        )
        for component in components
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate component identities")
    return ThirdPartyInventory(
        schema_version=raw["schema_version"],
        project_distribution=raw["project_distribution"],
        project_version=raw["project_version"],
        audit_date=raw["audit_date"],
        lockfile=raw["lockfile"],
        provider_config=raw["provider_config"],
        local_provider_ids=tuple(local_ids),
        components=components,
    )


def _package_index(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for package in lock["package"]:
        name = canonicalize_name(package["name"])
        if name in result:
            raise ValueError(f"multiple locked versions are unsupported: {name}")
        result[name] = package
    return result


def _closure(
    packages: dict[str, dict[str, Any]], roots: list[dict[str, Any]]
) -> dict[str, str]:
    pending = [(canonicalize_name(item["name"]), tuple(item.get("extra", item.get("extras", [])))) for item in roots]
    seen: dict[str, set[str]] = {}
    while pending:
        name, extras = pending.pop()
        requested = seen.setdefault(name, set())
        new_extras = set(extras) - requested
        if requested or name in seen and not new_extras and extras:
            if not new_extras:
                continue
        requested.update(extras)
        package = packages.get(name)
        if package is None:
            raise ValueError(f"locked dependency is missing: {name}")
        dependencies = list(package.get("dependencies", []))
        optional = package.get("optional-dependencies", {})
        for extra in new_extras or extras:
            dependencies.extend(optional.get(extra, []))
        for dependency in dependencies:
            pending.append(
                (
                    canonicalize_name(dependency["name"]),
                    tuple(dependency.get("extra", dependency.get("extras", []))),
                )
            )
    return {name: str(packages[name]["version"]) for name in sorted(seen)}


def locked_dependency_closure(root: Path, group: str) -> dict[str, str]:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = _package_index(lock)
    project = packages[PROJECT_DISTRIBUTION]
    runtime = _closure(packages, list(project.get("dependencies", [])))
    if group == "runtime":
        return runtime
    if group == "test":
        test = _closure(
            packages,
            list(project.get("dev-dependencies", {}).get("test", [])),
        )
        return {name: version for name, version in test.items() if name not in runtime}
    if group == "build":
        return {"hatchling": "1.32.0"}
    raise ValueError(f"unsupported dependency group: {group}")


def _identity(component: dict[str, object]) -> str:
    fields = []
    for key, label in (("version", "version"), ("tag", "tag"), ("commit", "commit"), ("image", "image")):
        value = component.get(key)
        if value:
            fields.append(f"{label} `{value}`")
    if not fields:
        fields.append(f"not applicable: {component['identity_not_applicable_reason']}")
    return "; ".join(fields)


def render_third_party_notices(inventory: ThirdPartyInventory) -> str:
    lines = [
        "# Third-Party Notices",
        "",
        "AI Mechanical 3DCAD Design Agent project-owned source is licensed under Apache-2.0.",
        "Third-party components retain their original ownership and licenses; this index does not relicense them.",
        "Every listed component is separately installed or external and is not distributed by this project.",
        "External service and catalog terms apply independently and do not grant rights to downloaded CAD data.",
        "",
    ]
    groups = [
        ("Installed runtime dependencies", lambda item: item["scope"] == "runtime"),
        ("Build and test dependencies", lambda item: item["scope"] in {"build", "test"}),
        ("External integrations", lambda item: item["relationship"] == "external_integration"),
        ("External services", lambda item: item["relationship"] == "external_service"),
        ("Catalog and web providers", lambda item: item["relationship"] == "catalog_web_provider"),
    ]
    for heading, predicate in groups:
        entries = sorted(
            (component for component in inventory.components if predicate(component)),
            key=lambda item: str(item["id"]),
        )
        if not entries:
            continue
        lines.extend([f"## {heading}", ""])
        for component in entries:
            lines.extend(
                [
                    f"### {component['name']}",
                    "",
                    f"- ID: `{component['id']}`",
                    f"- Identity: {_identity(component)}",
                    f"- SPDX: `{component['spdx']}`",
                    f"- Attribution: {component['attribution']}",
                    f"- Relationship: `{component['relationship']}`",
                    f"- Distribution: `{component['distribution']}`",
                    f"- Scope: `{component['scope']}`",
                    f"- Purpose: {component['purpose']}",
                    f"- Official source: {component['source_url']}",
                    f"- License or terms: {component['license_url']}",
                    f"- Evidence: {component['evidence_source']}",
                ]
            )
            if "declared_project_version" in component:
                lines.append(
                    f"- Declared project version: `{component['declared_project_version']}`"
                )
            if "committed_lock_version" in component:
                lines.append(
                    f"- Committed lock version: `{component['committed_lock_version']}`"
                )
            if "image_digest" in component:
                lines.append(f"- Image digest: `{component['image_digest']}`")
            if component.get("notes"):
                lines.append(f"- Notes: {component['notes']}")
            lines.extend([f"- Audited: `{component['audit_date'].isoformat()}`", ""])
    return "\n".join(lines).rstrip() + "\n"
