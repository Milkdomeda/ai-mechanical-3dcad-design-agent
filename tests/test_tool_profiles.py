from __future__ import annotations

import json

import pytest

from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.tool_profiles import (
    ALL_TOOL_NAMES,
    DESIGN_TOOL_NAMES,
    FAMILY_KNOWLEDGE_TOOL_NAMES,
    MAINTENANCE_TOOL_NAMES,
    TOOL_PROFILE_ENV,
    resolve_tool_profile,
)


def names(profile: str) -> set[str]:
    server = create_mcp(tool_profile=profile)
    return set(server._tool_manager._tools)


def test_all_profile_preserves_complete_public_inventory() -> None:
    assert names("all") == set(ALL_TOOL_NAMES)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("design", DESIGN_TOOL_NAMES),
        ("family-knowledge", FAMILY_KNOWLEDGE_TOOL_NAMES),
        ("maintenance", MAINTENANCE_TOOL_NAMES),
    ],
)
def test_reduced_profiles_expose_exact_inventories(
    profile: str, expected: frozenset[str]
) -> None:
    assert names(profile) == set(expected)


def test_design_profile_is_bounded_and_hides_expert_surfaces() -> None:
    visible = names("design")
    assert len(visible) <= 32
    assert {
        "design_job_obligations_resolve",
        "product_family_match",
        "design_knowledge_retrieve",
        "design_change_mutation_authorize",
        "design_validation_record",
        "design_delivery_approve",
    } <= visible
    assert not {
        "design_working_copy_create",
        "design_lesson_stage",
        "standard_part_catalog_enable",
        "projection_rebuild",
        "library_ingest_changes",
    } & visible


def test_explicit_profile_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOOL_PROFILE_ENV, "maintenance")
    assert names("design") == set(DESIGN_TOOL_NAMES)


def test_environment_selects_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOOL_PROFILE_ENV, "family-knowledge")
    server = create_mcp()
    assert set(server._tool_manager._tools) == set(FAMILY_KNOWLEDGE_TOOL_NAMES)


def test_unset_profile_preserves_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOOL_PROFILE_ENV, raising=False)
    assert resolve_tool_profile() == "all"


def test_unknown_profile_fails_closed() -> None:
    with pytest.raises(ValueError, match="must be design"):
        create_mcp(tool_profile="invented")


def test_design_profile_requires_engineering_scope_in_design_intent() -> None:
    class Service:
        def design_change_record(self, **kwargs: object) -> dict[str, object]:
            return kwargs

    server = create_mcp(service=Service(), tool_profile="design")
    invoke = server._tool_manager._tools["design_change_record"].fn
    draft = {
        "design_intent": {"summary": "mounting plate"},
        "architecture": {"kind": "single_part"},
        "key_interfaces": [],
        "user_constraints": [],
        "manufacturing_method": {"kind": "machining"},
        "material_constraints": [],
        "validation_requirements": [{"id": "shape-valid"}],
    }
    with pytest.raises(ValueError, match="engineering_scope"):
        invoke(
            "working-copy",
            "design_proposal",
            '[{"operation":"create"}]',
            "[]",
            "Create the approved plate.",
            json.dumps(draft),
        )
