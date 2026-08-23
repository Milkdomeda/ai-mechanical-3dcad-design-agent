from __future__ import annotations

from types import MappingProxyType

import pytest

from mechanical_design_agent.bootstrap_diagnostics import (
    CANONICAL_COMPONENTS,
    CAPABILITY_COMPONENTS,
    DOCTOR_PARTICIPANTS,
    STATUS_PARTICIPANTS,
    STATUS_SEVERITY,
    CapabilityRequest,
    ComponentDiagnostic,
    DiagnosticGateError,
    build_diagnostic_report,
    exit_code_for_status,
    guard_response,
)


EXPECTED_COMPONENTS = (
    "workspace_selection",
    "workspace_manifest",
    "managed_config_integrity",
    "actor_identity",
    "artifact_root",
    "product_family",
    "postgresql",
    "neo4j",
    "freecadcmd",
    "standard_part_sources",
    "package_resources",
)

EXPECTED_CAPABILITIES = {
    "bootstrap_init": (
        "workspace_selection",
        "managed_config_integrity",
    ),
    "config_inspection": (
        "workspace_selection",
        "workspace_manifest",
        "managed_config_integrity",
        "actor_identity",
        "package_resources",
    ),
    "postgres_migration": ("postgresql", "package_resources"),
    "database_bootstrap": (
        "workspace_selection",
        "workspace_manifest",
        "managed_config_integrity",
        "postgresql",
        "neo4j",
        "package_resources",
    ),
    "family_create_or_manage": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
    ),
    "design_job_workspace": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
        "product_family",
        "package_resources",
    ),
    "library_ingest": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
        "product_family",
        "freecadcmd",
        "artifact_root",
        "package_resources",
    ),
    "design_knowledge": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
        "neo4j",
    ),
    "cad_working_copy": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "artifact_root",
        "postgresql",
        "freecadcmd",
        "package_resources",
    ),
    "model_validation": (
        "workspace_selection",
        "workspace_manifest",
        "artifact_root",
        "freecadcmd",
        "package_resources",
    ),
    "artifact_registration": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "artifact_root",
        "postgresql",
    ),
    "projection": ("postgresql", "neo4j", "package_resources"),
    "standard_part_provider_list": ("package_resources",),
    "standard_part_config_inspection": (
        "workspace_selection",
        "workspace_manifest",
        "package_resources",
        "standard_part_sources",
    ),
    "standard_part_config_update": (
        "workspace_selection",
        "workspace_manifest",
        "managed_config_integrity",
        "package_resources",
        "standard_part_sources",
    ),
    "standard_part_catalog_write_or_reuse": (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
        "standard_part_sources",
    ),
}


def healthy_components() -> dict[str, ComponentDiagnostic]:
    return {
        name: ComponentDiagnostic(
            name=name,
            status="ok",
            code=f"{name.upper()}_READY",
            message=f"{name} is ready",
        )
        for name in CANONICAL_COMPONENTS
    }


def component(report: dict[str, object], name: str) -> dict[str, object]:
    components = report["components"]
    assert isinstance(components, list)
    return next(item for item in components if item["name"] == name)


def test_four_state_severity_order_is_strict_and_cli_compatible() -> None:
    assert dict(STATUS_SEVERITY) == {
        "ok": 0,
        "warning": 1,
        "setup_required": 2,
        "blocked": 3,
    }
    assert [exit_code_for_status(value) for value in STATUS_SEVERITY] == [0, 1, 2, 3]


def test_canonical_components_and_global_participants_match_contract() -> None:
    assert CANONICAL_COMPONENTS == EXPECTED_COMPONENTS
    assert STATUS_PARTICIPANTS == (
        "workspace_selection",
        "workspace_manifest",
        "managed_config_integrity",
        "actor_identity",
        "artifact_root",
        "package_resources",
    )
    assert DOCTOR_PARTICIPANTS == EXPECTED_COMPONENTS


def test_capability_registry_is_exact_and_immutable() -> None:
    assert dict(CAPABILITY_COMPONENTS) == EXPECTED_CAPABILITIES
    assert isinstance(CAPABILITY_COMPONENTS, MappingProxyType)
    with pytest.raises(TypeError):
        CAPABILITY_COMPONENTS["new"] = ()  # type: ignore[index]


def test_capability_request_adds_only_valid_conditional_components() -> None:
    request = CapabilityRequest(
        "family_create_or_manage",
        additional_components=("product_family",),
    )
    assert request.participants == (
        *EXPECTED_CAPABILITIES["family_create_or_manage"],
        "product_family",
    )
    assert CAPABILITY_COMPONENTS["family_create_or_manage"] == (
        "workspace_selection",
        "workspace_manifest",
        "actor_identity",
        "postgresql",
    )


@pytest.mark.parametrize(
    "capability_request",
    [
        CapabilityRequest("unknown"),
        CapabilityRequest(
            "family_create_or_manage",
            additional_components=("not_canonical",),
        ),
        CapabilityRequest(
            "family_create_or_manage",
            additional_components=("postgresql",),
        ),
        CapabilityRequest(
            "family_create_or_manage",
            additional_components=("product_family", "product_family"),
        ),
    ],
)
def test_invalid_capability_requests_are_rejected(
    capability_request: CapabilityRequest,
) -> None:
    with pytest.raises(ValueError):
        _ = capability_request.participants


def test_diagnostic_aggregation_is_ordered_and_deterministic() -> None:
    components = healthy_components()
    components["workspace_manifest"] = ComponentDiagnostic(
        "workspace_manifest",
        "warning",
        "MANIFEST_WARNING",
        "manifest warning",
    )
    components["actor_identity"] = ComponentDiagnostic(
        "actor_identity",
        "setup_required",
        "ACTOR_REQUIRED",
        "actor is required",
    )
    components["artifact_root"] = ComponentDiagnostic(
        "artifact_root",
        "blocked",
        "ARTIFACT_BLOCKED",
        "artifact root is unsafe",
        {"safe": False},
    )

    forward = build_diagnostic_report(
        kind="status",
        components=components,
        participants=STATUS_PARTICIPANTS,
    )
    reverse = build_diagnostic_report(
        kind="status",
        components=dict(reversed(list(components.items()))),
        participants=STATUS_PARTICIPANTS,
    )

    assert forward == reverse
    assert forward["status"] == {"overall": "blocked"}
    assert [item["name"] for item in forward["components"]] == list(
        CANONICAL_COMPONENTS
    )
    assert component(forward, "artifact_root")["details"] == {"safe": False}


def test_excluded_blocked_component_does_not_change_overall() -> None:
    components = healthy_components()
    components["postgresql"] = ComponentDiagnostic(
        "postgresql",
        "blocked",
        "POSTGRES_INVALID",
        "invalid URL",
    )
    report = build_diagnostic_report(
        kind="status",
        components=components,
        participants=STATUS_PARTICIPANTS,
    )
    assert report["status"] == {"overall": "ok"}
    assert component(report, "postgresql")["affects_overall"] is False


def test_component_details_are_immutable_and_copied_to_output() -> None:
    source = {"state": "empty"}
    diagnostic = ComponentDiagnostic(
        "product_family",
        "ok",
        "PRODUCT_FAMILY_EMPTY",
        "no product family configured",
        source,
    )
    source["state"] = "mutated"
    output = diagnostic.as_dict(affects_overall=False)
    assert output["details"] == {"state": "empty"}
    with pytest.raises(TypeError):
        diagnostic.details["state"] = "mutated"  # type: ignore[index]


@pytest.mark.parametrize(
    ("components", "participants"),
    [
        ({}, STATUS_PARTICIPANTS),
        ({**healthy_components(), "unknown": healthy_components()["postgresql"]}, STATUS_PARTICIPANTS),
        (healthy_components(), (*STATUS_PARTICIPANTS, "unknown")),
        (healthy_components(), (*STATUS_PARTICIPANTS, "artifact_root")),
    ],
)
def test_report_rejects_missing_unknown_or_duplicate_contract_entries(
    components: dict[str, ComponentDiagnostic],
    participants: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        build_diagnostic_report(
            kind="status",
            components=components,
            participants=participants,
        )


def test_guard_response_uses_first_highest_canonical_failure() -> None:
    components = healthy_components()
    components["workspace_manifest"] = ComponentDiagnostic(
        "workspace_manifest",
        "setup_required",
        "WORKSPACE_NOT_INITIALIZED",
        "initialize the workspace",
        {"next_steps": ["mechanical-design init --workspace <path>"]},
    )
    components["postgresql"] = ComponentDiagnostic(
        "postgresql",
        "setup_required",
        "POSTGRES_CREDENTIALS_REQUIRED",
        "configure PostgreSQL",
    )
    report = build_diagnostic_report(
        kind="capability",
        capability="family_create_or_manage",
        components=components,
        participants=("workspace_manifest", "postgresql"),
    )

    response = guard_response(report, capability="family_create_or_manage")

    assert response == {
        "schema_version": "MechanicalDesignSetupResponse/v1",
        "status": "setup_required",
        "code": "WORKSPACE_NOT_INITIALIZED",
        "message": "initialize the workspace",
        "capability": "family_create_or_manage",
        "next_steps": ["mechanical-design init --workspace <path>"],
        "diagnostics": report,
    }


@pytest.mark.parametrize("status", ["ok", "warning"])
def test_guard_response_rejects_nonblocking_reports(status: str) -> None:
    components = healthy_components()
    components["freecadcmd"] = ComponentDiagnostic(
        "freecadcmd",
        status,  # type: ignore[arg-type]
        "FREECAD_STATE",
        "FreeCAD state",
    )
    report = build_diagnostic_report(
        kind="capability",
        capability="model_validation",
        components=components,
        participants=("freecadcmd",),
    )
    with pytest.raises(ValueError):
        guard_response(report, capability="model_validation")


def test_diagnostic_gate_error_carries_the_structured_response() -> None:
    components = healthy_components()
    components["workspace_selection"] = ComponentDiagnostic(
        "workspace_selection",
        "setup_required",
        "WORKSPACE_NOT_INITIALIZED",
        "initialize the workspace",
    )
    report = build_diagnostic_report(
        kind="capability",
        capability="config_inspection",
        components=components,
        participants=("workspace_selection",),
    )
    response = guard_response(report, capability="config_inspection")
    error = DiagnosticGateError(response)
    assert error.response is response
    assert error.status == "setup_required"
    assert error.code == "WORKSPACE_NOT_INITIALIZED"
