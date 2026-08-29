from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP


TOOL_PROFILE_ENV = "MECH_DESIGN_MCP_TOOL_PROFILE"
TOOL_PROFILES = frozenset(
    {"design", "governed", "family-knowledge", "maintenance", "all"}
)

ALL_TOOL_NAMES = frozenset(
    {
        "design_approval_envelope_get",
        "design_assembly_completeness_validate",
        "design_change_applied",
        "design_change_audit_history",
        "design_change_close",
        "design_change_mutation_authorize",
        "design_change_record",
        "design_change_review",
        "design_confirmation_record",
        "design_context_build",
        "design_delivery_approve",
        "design_group_register",
        "design_job_close",
        "design_job_create",
        "design_job_get",
        "design_job_list",
        "design_job_new_working_copy_create",
        "design_job_obligations_resolve",
        "design_job_reopen",
        "design_job_resolve",
        "design_job_working_copy_create",
        "design_knowledge_retrieve",
        "design_record_result",
        "design_start",
        "design_lesson_approve",
        "design_lesson_audit_get",
        "design_lesson_get",
        "design_lesson_review_approve",
        "design_lesson_review_context",
        "design_lesson_review_no_publish",
        "design_lesson_review_prepare",
        "design_lesson_review_publish",
        "design_lesson_review_reject",
        "design_lesson_review_status",
        "design_lesson_revoke",
        "design_lesson_search",
        "design_lesson_stage",
        "design_lesson_staged_get",
        "design_lesson_supersede",
        "design_new_working_copy_create",
        "design_retrieval_receipt_get",
        "design_system_doctor",
        "design_system_status",
        "design_validation_record",
        "design_working_copy_create",
        "evidence_artifact_register",
        "family_bootstrap_get",
        "family_bootstrap_update",
        "family_compare_models",
        "family_create",
        "family_folder_confirm",
        "family_profile_get",
        "family_profile_propose",
        "family_profile_review",
        "job_get",
        "knowledge_propose_assertions",
        "knowledge_review",
        "knowledge_search",
        "learning_defer_targets",
        "learning_next_targets",
        "learning_record_exchange",
        "learning_start_session",
        "library_ingest_changes",
        "library_register",
        "library_scan",
        "model_get_analysis",
        "model_identity_confirm",
        "product_family_inventory",
        "product_family_match",
        "product_family_onboarding_analyze",
        "product_family_onboarding_publish",
        "product_family_onboarding_review",
        "product_family_onboarding_start",
        "product_family_onboarding_status",
        "projection_rebuild",
        "projection_sync",
        "standard_part_catalog_disable",
        "standard_part_catalog_enable",
        "standard_part_download_register",
        "standard_part_providers_get",
        "standard_part_sources_status",
        "subfamily_get",
        "subfamily_propose",
        "subfamily_review",
        "workspace_product_family_active",
        "workspace_product_family_create",
        "workspace_product_family_list",
        "workspace_product_family_set_default",
    }
)

DESIGN_TOOL_NAMES = frozenset(
    {
        "design_system_status",
        "design_start",
        "design_knowledge_retrieve",
        "design_record_result",
        "standard_part_providers_get",
        "standard_part_sources_status",
        "standard_part_download_register",
    }
)

GOVERNED_TOOL_NAMES = frozenset(
    {
        "design_system_status",
        "design_job_create",
        "design_job_list",
        "design_job_get",
        "design_job_resolve",
        "design_job_close",
        "design_job_reopen",
        "product_family_inventory",
        "product_family_match",
        "design_job_working_copy_create",
        "design_job_new_working_copy_create",
        "design_job_obligations_resolve",
        "design_knowledge_retrieve",
        "design_retrieval_receipt_get",
        "design_change_record",
        "design_change_review",
        "design_approval_envelope_get",
        "design_change_mutation_authorize",
        "design_change_applied",
        "design_change_close",
        "design_confirmation_record",
        "design_validation_record",
        "design_assembly_completeness_validate",
        "design_delivery_approve",
        "standard_part_providers_get",
        "standard_part_sources_status",
        "standard_part_download_register",
        "design_lesson_review_context",
        "design_lesson_review_prepare",
        "design_lesson_review_status",
        "design_lesson_review_publish",
        "design_lesson_review_no_publish",
    }
)

FAMILY_KNOWLEDGE_TOOL_NAMES = frozenset(
    {
        "design_system_status",
        "design_system_doctor",
        "workspace_product_family_list",
        "workspace_product_family_active",
        "design_job_create",
        "design_job_list",
        "design_job_get",
        "design_job_resolve",
        "design_job_close",
        "design_job_reopen",
        "product_family_onboarding_start",
        "product_family_onboarding_analyze",
        "product_family_onboarding_review",
        "product_family_onboarding_publish",
        "product_family_onboarding_status",
        "library_register",
        "library_scan",
        "family_folder_confirm",
        "library_ingest_changes",
        "evidence_artifact_register",
        "job_get",
        "model_get_analysis",
        "learning_start_session",
        "learning_next_targets",
        "learning_record_exchange",
        "learning_defer_targets",
        "knowledge_propose_assertions",
        "knowledge_review",
        "knowledge_search",
        "model_identity_confirm",
        "family_compare_models",
        "subfamily_propose",
        "subfamily_review",
        "subfamily_get",
        "family_profile_propose",
        "family_profile_review",
        "family_profile_get",
    }
)

MAINTENANCE_TOOL_NAMES = frozenset(
    {
        "design_system_status",
        "design_system_doctor",
        "workspace_product_family_list",
        "workspace_product_family_create",
        "workspace_product_family_set_default",
        "workspace_product_family_active",
        "family_bootstrap_get",
        "family_bootstrap_update",
        "design_group_register",
        "family_create",
        "standard_part_sources_status",
        "standard_part_catalog_enable",
        "standard_part_catalog_disable",
        "design_lesson_get",
        "design_lesson_audit_get",
        "design_lesson_supersede",
        "design_lesson_revoke",
        "design_change_audit_history",
        "projection_sync",
        "projection_rebuild",
    }
)

PROFILE_TOOL_NAMES = {
    "design": DESIGN_TOOL_NAMES,
    "governed": GOVERNED_TOOL_NAMES,
    "family-knowledge": FAMILY_KNOWLEDGE_TOOL_NAMES,
    "maintenance": MAINTENANCE_TOOL_NAMES,
    "all": ALL_TOOL_NAMES,
}


def resolve_tool_profile(
    requested: str | None = None, *, environ: dict[str, str] | None = None
) -> str:
    environment = os.environ if environ is None else environ
    value = requested if requested is not None else environment.get(TOOL_PROFILE_ENV, "design")
    normalized = str(value).strip().casefold()
    if normalized not in TOOL_PROFILES:
        raise ValueError(
            "MECH_DESIGN_MCP_TOOL_PROFILE must be design, governed, "
            "family-knowledge, maintenance, or all"
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
