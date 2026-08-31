from __future__ import annotations

import pytest

from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.tool_profiles import (
    DESIGN_TOOL_NAMES,
    KNOWLEDGE_ADMIN_TOOL_NAMES,
    TOOL_PROFILE_ENV,
    resolve_tool_profile,
)


def names(profile: str) -> set[str]:
    return set(create_mcp(tool_profile=profile)._tool_manager._tools)


def test_design_surface_exposes_the_complete_normal_flow() -> None:
    assert names("design") == set(DESIGN_TOOL_NAMES)
    assert DESIGN_TOOL_NAMES == {
        "design_system_status",
        "design_start",
        "design_status",
        "design_knowledge_retrieve",
        "design_record_result",
        "design_confirm",
        "design_lesson_decide",
        "standard_part_providers_get",
        "standard_part_sources_status",
        "standard_part_download_register",
    }


def test_knowledge_administration_is_a_separate_surface() -> None:
    assert names("knowledge-admin") == set(KNOWLEDGE_ADMIN_TOOL_NAMES)
    assert not {
        "design_start",
        "design_record_result",
        "design_confirm",
        "design_lesson_decide",
    } & KNOWLEDGE_ADMIN_TOOL_NAMES


def test_design_is_the_default_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOOL_PROFILE_ENV, raising=False)
    assert resolve_tool_profile() == "design"
    assert set(create_mcp()._tool_manager._tools) == set(DESIGN_TOOL_NAMES)


def test_environment_can_select_knowledge_administration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOOL_PROFILE_ENV, "knowledge-admin")
    assert set(create_mcp()._tool_manager._tools) == set(
        KNOWLEDGE_ADMIN_TOOL_NAMES
    )


@pytest.mark.parametrize("invalid", ["unknown", "all", "maintenance"])
def test_unknown_surfaces_fail_closed(invalid: str) -> None:
    with pytest.raises(ValueError, match="design or knowledge-admin"):
        create_mcp(tool_profile=invalid)
