from __future__ import annotations

import copy

import pytest

from mechanical_design_agent.engineering_obligations import (
    assembly_triggers,
    build_obligation_read_model,
    engineering_scope_sha256,
    require_obligation_gate,
    standard_part_triggers,
    validate_engineering_scope,
    validate_obligation_decision,
)


def plate_scope() -> dict:
    return {
        "schema_version": "EngineeringScope/v1",
        "deliverable_kind": "single_part",
        "component_count": 1,
        "motion_present": False,
        "assembly_interfaces": [],
        "component_plan": [
            {
                "component_id": "mounting-plate",
                "category": "machined_plate",
                "sourcing_class": "custom",
                "included_in_delivery": True,
            }
        ],
    }


def decision(kind: str, outcome: str, *, evidence: list[str] | None = None) -> dict:
    return {
        "obligation_kind": kind,
        "outcome": outcome,
        "resolution_level": "screening",
        "rationale": "Explicit engineering screening conclusion.",
        "evidence_refs": evidence or [],
    }


def test_simple_plate_resolves_quickly_without_complex_workflows() -> None:
    scope = plate_scope()
    standard = validate_obligation_decision(
        decision("standard_parts_assessment", "not_applicable"), scope
    )
    assembly = validate_obligation_decision(
        decision("assembly_assessment", "not_applicable"), scope
    )

    read_model = build_obligation_read_model(
        scope=scope,
        family_outcome="no_match",
        knowledge_outcome="completed_no_matches",
        standard_parts_decision=standard,
        assembly_decision=assembly,
    )

    assert read_model["open_obligations"] == []
    assert read_model["blocked_actions"] == {}
    assert "design_change_mutation_authorize" in read_model["allowed_actions"]


def test_scope_hash_is_canonical_and_changes_with_delivery_scope() -> None:
    scope = plate_scope()
    reordered = {key: scope[key] for key in reversed(scope)}
    assert engineering_scope_sha256(reordered) == engineering_scope_sha256(scope)

    changed = copy.deepcopy(scope)
    changed["component_plan"][0]["included_in_delivery"] = False
    assert engineering_scope_sha256(changed) != engineering_scope_sha256(scope)


def test_delivered_fastener_reopens_standard_parts_assessment() -> None:
    scope = plate_scope()
    scope["component_count"] = 2
    scope["component_plan"].append(
        {
            "component_id": "m5-fastener",
            "category": "fastener",
            "sourcing_class": "standard_candidate",
            "included_in_delivery": True,
        }
    )

    assert standard_part_triggers(scope) == [
        "m5-fastener:sourcing_class",
        "m5-fastener:standard_category",
    ]
    with pytest.raises(ValueError, match="conflicts with scope triggers"):
        validate_obligation_decision(
            decision("standard_parts_assessment", "not_applicable"), scope
        )


def test_linear_actuator_expands_standard_parts_and_assembly() -> None:
    scope = {
        "schema_version": "EngineeringScope/v1",
        "deliverable_kind": "assembly",
        "component_count": 3,
        "motion_present": True,
        "assembly_interfaces": ["guide-to-frame", "motor-to-coupling"],
        "component_plan": [
            {
                "component_id": "frame",
                "category": "machined_frame",
                "sourcing_class": "custom",
                "included_in_delivery": True,
            },
            {
                "component_id": "linear-guide",
                "category": "guide_rail",
                "sourcing_class": "standard_candidate",
                "included_in_delivery": True,
            },
            {
                "component_id": "motor",
                "category": "motor",
                "sourcing_class": "standard_candidate",
                "included_in_delivery": True,
            },
        ],
    }

    assert standard_part_triggers(scope)
    assert assembly_triggers(scope) == [
        "deliverable_kind",
        "component_count",
        "motion_present",
        "assembly_interfaces",
    ]
    pending = validate_obligation_decision(
        decision("assembly_assessment", "required_pending"), scope
    )
    assert pending["outcome"] == "required_pending"


def test_expanded_standard_parts_requires_evidence() -> None:
    scope = plate_scope()
    scope["component_plan"][0].update(
        category="bearing", sourcing_class="standard_candidate"
    )
    with pytest.raises(ValueError, match="requires evidence"):
        validate_obligation_decision(
            decision("standard_parts_assessment", "candidates_resolved"), scope
        )
    resolved = validate_obligation_decision(
        decision(
            "standard_parts_assessment",
            "candidates_resolved",
            evidence=["standard-part-record:123"],
        ),
        scope,
    )
    assert resolved["evidence_refs"] == ["standard-part-record:123"]


def test_read_model_allows_independent_actions_without_sequence() -> None:
    read_model = build_obligation_read_model(
        scope=plate_scope(),
        family_outcome=None,
        knowledge_outcome=None,
        standard_parts_decision=None,
        assembly_decision=None,
    )
    assert set(read_model["allowed_actions"]) == {
        "product_family_match",
        "design_knowledge_retrieve",
        "design_job_obligations_resolve",
    }
    assert set(read_model["blocked_actions"]["design_change_mutation_authorize"]) == {
        "product_family_resolution",
        "knowledge_retrieval",
        "standard_parts_assessment",
        "assembly_assessment",
    }


def test_mutation_gate_accepts_screened_simple_part() -> None:
    require_obligation_gate(
        scope=plate_scope(),
        family_outcome="not_configured",
        knowledge_outcome="completed_no_matches",
        standard_parts_decision=decision(
            "standard_parts_assessment", "not_applicable"
        ),
        assembly_decision=decision("assembly_assessment", "not_applicable"),
    )


def test_delivery_gate_keeps_expanded_assembly_open_until_passed() -> None:
    scope = plate_scope()
    scope.update(deliverable_kind="assembly", component_count=2)
    scope["component_plan"].append(
        {
            "component_id": "bearing",
            "category": "bearing",
            "sourcing_class": "standard_selected",
            "included_in_delivery": True,
        }
    )
    standard = decision(
        "standard_parts_assessment",
        "candidates_resolved",
        evidence=["standard-part-record:123"],
    )
    pending = decision("assembly_assessment", "required_pending")
    require_obligation_gate(
        scope=scope,
        family_outcome="no_match",
        knowledge_outcome="completed_matches",
        standard_parts_decision=standard,
        assembly_decision=pending,
    )
    with pytest.raises(ValueError, match="passed assembly assessment"):
        require_obligation_gate(
            scope=scope,
            family_outcome="no_match",
            knowledge_outcome="completed_matches",
            standard_parts_decision=standard,
            assembly_decision=pending,
            for_delivery=True,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("component_count", True),
        ("motion_present", 0),
        ("deliverable_kind", "part"),
        ("assembly_interfaces", [""]),
    ],
)
def test_scope_validation_rejects_ambiguous_values(field: str, value: object) -> None:
    scope = plate_scope()
    scope[field] = value
    with pytest.raises(ValueError):
        validate_engineering_scope(scope)
