from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP


TOOL_PROFILE_ENV = "MECH_DESIGN_MCP_TOOL_PROFILE"
TOOL_PROFILES = frozenset({"design", "knowledge-admin"})

DESIGN_TOOL_NAMES = frozenset(
    {
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
)

KNOWLEDGE_ADMIN_TOOL_NAMES = frozenset(
    {
        "design_system_status",
        "product_family_onboarding_start",
        "product_family_onboarding_analyze",
        "product_family_onboarding_review",
        "product_family_onboarding_publish",
        "product_family_onboarding_status",
        "knowledge_search",
        "knowledge_review",
        "design_lesson_search",
        "design_lesson_get",
        "design_lesson_supersede",
        "design_lesson_revoke",
        "projection_sync",
        "projection_rebuild",
    }
)

ALL_TOOL_NAMES = DESIGN_TOOL_NAMES | KNOWLEDGE_ADMIN_TOOL_NAMES
PROFILE_TOOL_NAMES = {
    "design": DESIGN_TOOL_NAMES,
    "knowledge-admin": KNOWLEDGE_ADMIN_TOOL_NAMES,
}


def resolve_tool_profile(
    requested: str | None = None, *, environ: dict[str, str] | None = None
) -> str:
    environment = os.environ if environ is None else environ
    value = requested if requested is not None else environment.get(
        TOOL_PROFILE_ENV, "design"
    )
    normalized = str(value).strip().casefold()
    if normalized not in TOOL_PROFILES:
        raise ValueError(
            "MECH_DESIGN_MCP_TOOL_PROFILE must be design or knowledge-admin"
        )
    return normalized


def tool_names_for_profile(profile: str) -> frozenset[str]:
    return PROFILE_TOOL_NAMES[resolve_tool_profile(profile, environ={})]


class ProfiledToolRegistrar:
    """Filter FastMCP registration without mutating framework internals."""

    def __init__(self, server: FastMCP, profile: str):
        self.server = server
        self.profile = resolve_tool_profile(profile, environ={})
        self.allowed = tool_names_for_profile(self.profile)
        self.encountered: set[str] = set()

    def tool(
        self,
        *,
        name: str | None = None,
        profiles: frozenset[str] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            public_name = name or function.__name__
            if public_name not in ALL_TOOL_NAMES:
                raise RuntimeError(
                    f"MCP tool is missing exposure metadata: {public_name}"
                )
            self.encountered.add(public_name)
            profile_selected = profiles is None or self.profile in profiles
            if public_name in self.allowed and profile_selected:
                return self.server.tool(name=public_name)(function)
            return function

        return decorate

    def assert_complete(self) -> None:
        missing = ALL_TOOL_NAMES - self.encountered
        unexpected = self.encountered - ALL_TOOL_NAMES
        if missing or unexpected:
            raise RuntimeError(
                "MCP exposure inventory mismatch: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )


__all__ = [
    "ALL_TOOL_NAMES",
    "DESIGN_TOOL_NAMES",
    "KNOWLEDGE_ADMIN_TOOL_NAMES",
    "ProfiledToolRegistrar",
    "TOOL_PROFILE_ENV",
    "TOOL_PROFILES",
    "resolve_tool_profile",
    "tool_names_for_profile",
]
