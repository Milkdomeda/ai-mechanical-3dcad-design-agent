from __future__ import annotations

import json
from pathlib import Path

import pytest

from third_party_licensing_helpers import (
    load_third_party_inventory,
    locked_dependency_closure,
    render_third_party_notices,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNTIME = {
    "annotated-doc": "0.0.5",
    "annotated-types": "0.8.0",
    "anyio": "4.14.2",
    "attrs": "26.1.0",
    "certifi": "2026.7.22",
    "cffi": "2.1.1",
    "click": "8.4.2",
    "colorama": "0.4.6",
    "cryptography": "50.0.0",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "httpx": "0.28.1",
    "httpx-sse": "0.4.3",
    "idna": "3.18",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "markdown-it-py": "4.2.0",
    "mcp": "1.29.0",
    "mdurl": "0.1.2",
    "neo4j": "6.2.0",
    "psycopg": "3.3.4",
    "psycopg-binary": "3.3.4",
    "pycparser": "3.0",
    "pydantic": "2.13.4",
    "pydantic-core": "2.46.4",
    "pydantic-settings": "2.15.0",
    "pygments": "2.20.0",
    "pyjwt": "2.13.0",
    "python-dotenv": "1.2.2",
    "python-multipart": "0.0.32",
    "pytz": "2026.3.post1",
    "pywin32": "312",
    "referencing": "0.37.0",
    "rich": "15.0.0",
    "rpds-py": "2026.6.3",
    "shellingham": "1.5.4",
    "sse-starlette": "3.4.8",
    "starlette": "1.4.1",
    "typer": "0.27.1",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.2",
    "tzdata": "2026.3",
    "uvicorn": "0.52.1",
}
EXPECTED_TEST = {
    "iniconfig": "2.3.0",
    "packaging": "26.3",
    "pluggy": "1.6.0",
    "pytest": "9.1.1",
}


def components_by_id() -> dict[str, dict[str, object]]:
    inventory = load_third_party_inventory(PROJECT_ROOT)
    return {str(component["id"]): component for component in inventory.components}


def test_locked_dependency_closures_are_the_audited_release_set() -> None:
    assert locked_dependency_closure(PROJECT_ROOT, "runtime") == EXPECTED_RUNTIME
    assert locked_dependency_closure(PROJECT_ROOT, "test") == EXPECTED_TEST
    assert locked_dependency_closure(PROJECT_ROOT, "build") == {"hatchling": "1.32.0"}


def test_inventory_covers_dependency_closures_and_directness() -> None:
    components = components_by_id()
    for scope, expected in (
        ("runtime", EXPECTED_RUNTIME),
        ("test", EXPECTED_TEST),
        ("build", {"hatchling": "1.32.0"}),
    ):
        actual = {
            str(component["id"]): str(component["version"])
            for component in components.values()
            if component["scope"] == scope
            and component["relationship"] == "installed_dependency"
        }
        assert actual == expected
    direct_runtime = {
        component_id
        for component_id, component in components.items()
        if component["scope"] == "runtime" and component.get("direct") is True
    }
    assert direct_runtime == {
        "mcp",
        "neo4j",
        "psycopg",
        "psycopg-binary",
        "pywin32",
    }
    assert "direct-through-extra" in str(components["psycopg-binary"]["notes"])


def test_external_integrations_and_services_retain_exact_identity() -> None:
    components = components_by_id()
    assert components["freecad"]["version"] == "1.1.1"
    assert "Historical macOS acceptance only" in str(
        components["freecad"]["notes"]
    )
    freecad_windows = components["freecad-1-1-3-windows"]
    assert freecad_windows["version"] == "1.1.3"
    assert freecad_windows["commit"] == (
        "145529fe741292ff0b3977a01195bf0247425794"
    )
    assert freecad_windows["relationship"] == "external_integration"
    assert freecad_windows["distribution"] == "not_distributed_by_project"
    assert freecad_windows["source_url"] == (
        "https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.3"
    )
    freecad_mcp = components["freecad-mcp"]
    assert freecad_mcp["relationship"] == "external_integration"
    assert freecad_mcp["distribution"] == "not_distributed_by_project"
    assert freecad_mcp["commit"] == "7667e272e1db669ff61dd5411fb4f622691f2dbc"
    assert freecad_mcp["declared_project_version"] == "0.1.19"
    assert freecad_mcp["committed_lock_version"] == "0.1.17"
    assert "version" not in freecad_mcp
    assert components["freecad-fasteners-workbench"]["spdx"] == "GPL-2.0-or-later"
    assert components["freecad-gears-workbench"]["spdx"] == "GPL-3.0-or-later"
    assert components["pgvector-service"]["image"] == "pgvector/pgvector:0.8.5-pg18"
    assert components["neo4j-server"]["image"] == "neo4j:2026.06.0"
    assert components["pgvector-service"]["image_digest"] == (
        "sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0"
    )
    assert components["neo4j-server"]["image_digest"] == (
        "sha256:42fd5b9ead4dd4211f6f91bd831c358e4e2117367d04633fbf88682ca4792b30"
    )
    assert components["postgresql-server"]["relationship"] == "external_service"


def test_every_configured_external_provider_is_classified() -> None:
    inventory = load_third_party_inventory(PROJECT_ROOT)
    components = components_by_id()
    provider_config = json.loads(
        (PROJECT_ROOT / inventory.provider_config).read_text(encoding="utf-8")
    )
    configured = {provider["id"] for provider in provider_config["providers"]}
    mapped = {
        provider_id
        for component in components.values()
        for provider_id in component.get("provider_ids", [])
    }
    assert configured == mapped | set(inventory.local_provider_ids)
    for manufacturer in ("thomson", "hiwin", "thk", "pmi", "tbi", "nsk", "bosch-rexroth"):
        component = components[f"manufacturer-{manufacturer}"]
        assert component["relationship"] == "catalog_web_provider"
        assert "manufacturer-official" in component["provider_ids"]


def test_notice_is_deterministically_derived_from_inventory() -> None:
    inventory = load_third_party_inventory(PROJECT_ROOT)
    expected = render_third_party_notices(inventory)
    assert (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8") == expected
    assert "### FreeCAD 1.1.3 (Windows validation)" in expected
    assert (
        "- Identity: version `1.1.3`; commit "
        "`145529fe741292ff0b3977a01195bf0247425794`"
    ) in expected
    assert "- Image digest: `sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0`" in expected
    assert "- Image digest: `sha256:42fd5b9ead4dd4211f6f91bd831c358e4e2117367d04633fbf88682ca4792b30`" in expected


def _mutate_component_by_id(original: str, component_id: str, transform) -> str:
    marker = f'id = "{component_id}"'
    prefix, suffix = original.split(marker, 1)
    if "[[components]]" in suffix:
        component, remainder = suffix.split("[[components]]", 1)
        return prefix + marker + transform(component) + "[[components]]" + remainder
    return prefix + marker + transform(suffix)


@pytest.mark.parametrize("component_id", ["pgvector-service", "neo4j-server"])
def test_image_backed_external_services_require_digest(
    tmp_path: Path,
    component_id: str,
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    mutated = _mutate_component_by_id(
        original,
        component_id,
        lambda block: "\n".join(
            line for line in block.splitlines() if not line.startswith("image_digest = ")
        )
        + "\n",
    )
    (tmp_path / "third-party-components.toml").write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match="requires image_digest"):
        load_third_party_inventory(tmp_path)


def test_image_digest_is_lowercase_sha256_and_image_service_only(
    tmp_path: Path,
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    malformed = original.replace(
        "sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0",
        "SHA256:not-a-digest",
        1,
    )
    (tmp_path / "third-party-components.toml").write_text(malformed, encoding="utf-8")
    with pytest.raises(ValueError, match="lowercase image SHA-256"):
        load_third_party_inventory(tmp_path)

    invalid_relationship = _mutate_first_component(
        original,
        lambda block: block + "image_digest = \"sha256:" + "0" * 64 + "\"\n",
    )
    (tmp_path / "third-party-components.toml").write_text(
        invalid_relationship,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="image-backed external services"):
        load_third_party_inventory(tmp_path)


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("relationship = \"bundled\"", "relationship"),
        ("distribution = \"bundled\"", "distribution"),
        ("source_url = \"http://example.com/source\"", "HTTPS"),
        ("spdx = \"Apache-2.0\"", "GPL-2.0-or-later"),
    ],
)
def test_schema_rejects_invalid_component_contract(
    tmp_path: Path, fragment: str, message: str
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    if fragment.startswith("relationship"):
        mutated = original.replace('relationship = "installed_dependency"', fragment, 1)
    elif fragment.startswith("distribution"):
        mutated = original.replace('distribution = "not_distributed_by_project"', fragment, 1)
    elif fragment.startswith("source_url"):
        first_source = next(line for line in original.splitlines() if line.startswith("source_url = "))
        mutated = original.replace(first_source, fragment, 1)
    else:
        marker = 'id = "freecad-fasteners-workbench"'
        prefix, suffix = original.split(marker, 1)
        suffix = suffix.replace('spdx = "GPL-2.0-or-later"', fragment, 1)
        mutated = prefix + marker + suffix
    (tmp_path / "third-party-components.toml").write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_third_party_inventory(tmp_path)


def _mutate_first_component(original: str, transform) -> str:
    prefix, remainder = original.split("[[components]]", 1)
    component, suffix = remainder.split("[[components]]", 1)
    return prefix + "[[components]]" + transform(component) + "[[components]]" + suffix


@pytest.mark.parametrize(
    "field",
    [
        "relationship",
        "distribution",
        "source_url",
        "license_url",
        "evidence_source",
        "audit_date",
    ],
)
def test_schema_rejects_missing_required_component_fields(
    tmp_path: Path, field: str
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")

    def remove_field(component: str) -> str:
        return "\n".join(
            line for line in component.splitlines() if not line.startswith(f"{field} = ")
        ) + "\n"

    mutated = _mutate_first_component(original, remove_field)
    (tmp_path / "third-party-components.toml").write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match="missing required fields"):
        load_third_party_inventory(tmp_path)


def test_schema_rejects_duplicate_ids_and_duplicate_identities(tmp_path: Path) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    first = "[[components]]" + original.split("[[components]]", 2)[1]

    duplicate_id = original.rstrip() + "\n\n" + first
    (tmp_path / "third-party-components.toml").write_text(
        duplicate_id, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate component ids"):
        load_third_party_inventory(tmp_path)

    duplicate_identity = original.rstrip() + "\n\n" + first.replace(
        'id = "actions-checkout"', 'id = "actions-checkout-copy"', 1
    )
    (tmp_path / "third-party-components.toml").write_text(
        duplicate_identity, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate component identities"):
        load_third_party_inventory(tmp_path)


def test_schema_rejects_noassertion_outside_catalog_boundary(tmp_path: Path) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    mutated = original.replace('spdx = "MIT"', 'spdx = "NOASSERTION"', 1)
    (tmp_path / "third-party-components.toml").write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match="NOASSERTION is catalog-only"):
        load_third_party_inventory(tmp_path)


def test_schema_rejects_catalog_terms_described_as_open_source_license(
    tmp_path: Path,
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    marker = 'id = "step-parts"'
    prefix, suffix = original.split(marker, 1)
    suffix = suffix.replace(
        "this is not an open-source software license",
        "this is an open-source software license",
        1,
    )
    (tmp_path / "third-party-components.toml").write_text(
        prefix + marker + suffix, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="service-terms boundary"):
        load_third_party_inventory(tmp_path)


@pytest.mark.parametrize(
    ("addition_or_replacement", "message"),
    [
        ('version = "0.1.19"\n', "must not normalize"),
        ('tag = "v0.1.19"', "no tag was approved"),
    ],
)
def test_schema_preserves_freecad_mcp_upstream_version_inconsistency(
    tmp_path: Path, addition_or_replacement: str, message: str
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    marker = 'id = "freecad-mcp"'
    prefix, suffix = original.split(marker, 1)
    if addition_or_replacement.startswith("version"):
        suffix = suffix.replace('name = "FreeCAD GUI MCP"\n', 'name = "FreeCAD GUI MCP"\n' + addition_or_replacement, 1)
    else:
        suffix = suffix.replace('tag = "none"', addition_or_replacement, 1)
    (tmp_path / "third-party-components.toml").write_text(
        prefix + marker + suffix, encoding="utf-8"
    )
    with pytest.raises(ValueError, match=message):
        load_third_party_inventory(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown_inventory_key = true\n", "unknown fields"),
        ('scope = "unsupported"', "scope is invalid"),
        ('attribution = ""', "must not be blank"),
        ('source_url = "../local-source"', "HTTPS"),
        ('identity_not_applicable_reason = "also exact"\n', "mixes an exact identity"),
    ],
)
def test_schema_rejects_unknown_blank_local_and_ambiguous_values(
    tmp_path: Path, mutation: str, message: str
) -> None:
    original = (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    if mutation.startswith("unknown_inventory_key"):
        mutated = mutation + original
    elif mutation.startswith("identity_not_applicable_reason"):
        mutated = _mutate_first_component(original, lambda block: "\n" + mutation + block)
    else:
        field = mutation.split(" = ", 1)[0]
        mutated = _mutate_first_component(
            original,
            lambda block: "\n".join(
                mutation if line.startswith(f"{field} = ") else line
                for line in block.splitlines()
            )
            + "\n",
        )
    (tmp_path / "third-party-components.toml").write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_third_party_inventory(tmp_path)
