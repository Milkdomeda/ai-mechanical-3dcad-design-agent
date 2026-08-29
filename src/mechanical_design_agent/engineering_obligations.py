from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping

from .models import canonical_json, require_safe_id


SOURCING_CLASSES = frozenset(
    {"custom", "standard_candidate", "standard_selected", "unresolved"}
)
STANDARD_PART_CATEGORIES = frozenset(
    {
        "actuator",
        "bearing",
        "bolt",
        "coupling",
        "fastener",
        "flange",
        "gear",
        "guide_rail",
        "key",
        "motor",
        "nut",
        "pin",
        "roller",
        "screw",
        "servo",
        "structural_profile",
        "washer",
        "worm",
    }
)
OBLIGATION_OUTCOMES = {
    "standard_parts_assessment": frozenset(
        {
            "not_applicable",
            "no_candidates",
            "candidates_resolved",
            "approved_custom_exception",
        }
    ),
    "assembly_assessment": frozenset(
        {"not_applicable", "required_pending", "required_passed"}
    ),
}
RESOLUTION_LEVELS = frozenset({"screening", "expanded"})

_SCOPE_FIELDS = {
    "schema_version",
    "deliverable_kind",
    "component_count",
    "motion_present",
    "assembly_interfaces",
    "component_plan",
}
_COMPONENT_FIELDS = {
    "component_id",
    "category",
    "sourcing_class",
    "included_in_delivery",
}
_DECISION_FIELDS = {
    "obligation_kind",
    "outcome",
    "resolution_level",
    "rationale",
    "evidence_refs",
}


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("engineering obligation data must be finite JSON") from exc


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    return value.strip()


def validate_engineering_scope(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return one strict canonical EngineeringScope/v1 JSON object."""

    if not isinstance(raw, Mapping):
        raise ValueError("engineering scope must be an object")
    scope = _json_copy(dict(raw))
    if set(scope) != _SCOPE_FIELDS:
        raise ValueError("engineering scope fields are invalid")
    if scope["schema_version"] != "EngineeringScope/v1":
        raise ValueError("engineering scope schema_version is invalid")
    if scope["deliverable_kind"] not in {"single_part", "assembly", "unknown"}:
        raise ValueError("engineering scope deliverable_kind is invalid")
    if type(scope["component_count"]) is not int or scope["component_count"] < 1:
        raise ValueError("engineering scope component_count must be a positive integer")
    if type(scope["motion_present"]) is not bool:
        raise ValueError("engineering scope motion_present must be a boolean")
    interfaces = scope["assembly_interfaces"]
    if not isinstance(interfaces, list) or any(
        not isinstance(value, str) or not value.strip() for value in interfaces
    ):
        raise ValueError("engineering scope assembly_interfaces must be nonblank strings")
    if len(interfaces) != len(set(interfaces)):
        raise ValueError("engineering scope assembly_interfaces must be unique")

    components = scope["component_plan"]
    if not isinstance(components, list) or not components:
        raise ValueError("engineering scope component_plan must be nonempty")
    component_ids: set[str] = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != _COMPONENT_FIELDS:
            raise ValueError("engineering scope component fields are invalid")
        component_id = _nonblank(component["component_id"], "component_id")
        require_safe_id(component_id, "component_id")
        if component_id in component_ids:
            raise ValueError("engineering scope component_id values must be unique")
        component_ids.add(component_id)
        component["component_id"] = component_id
        component["category"] = _nonblank(component["category"], "component category")
        if component["sourcing_class"] not in SOURCING_CLASSES:
            raise ValueError("engineering scope sourcing_class is invalid")
        if type(component["included_in_delivery"]) is not bool:
            raise ValueError("included_in_delivery must be a boolean")
    if scope["component_count"] != len(components):
        raise ValueError("component_count must equal the component_plan length")
    return scope


def engineering_scope_sha256(raw: Mapping[str, Any]) -> str:
    scope = validate_engineering_scope(raw)
    return hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()


def standard_part_triggers(scope: Mapping[str, Any]) -> list[str]:
    validated = validate_engineering_scope(scope)
    triggers: list[str] = []
    for component in validated["component_plan"]:
        component_id = component["component_id"]
        if component["sourcing_class"] in {
            "standard_candidate",
            "standard_selected",
            "unresolved",
        }:
            triggers.append(f"{component_id}:sourcing_class")
        if (
            component["included_in_delivery"]
            and component["category"].casefold() in STANDARD_PART_CATEGORIES
        ):
            triggers.append(f"{component_id}:standard_category")
    return list(dict.fromkeys(triggers))


def assembly_triggers(scope: Mapping[str, Any]) -> list[str]:
    validated = validate_engineering_scope(scope)
    triggers: list[str] = []
    if validated["deliverable_kind"] != "single_part":
        triggers.append("deliverable_kind")
    if validated["component_count"] != 1:
        triggers.append("component_count")
    if validated["motion_present"]:
        triggers.append("motion_present")
    if validated["assembly_interfaces"]:
        triggers.append("assembly_interfaces")
    return triggers


def validate_obligation_decision(
    raw: Mapping[str, Any], scope: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one adaptive screening/expanded conclusion for an exact scope."""

    if not isinstance(raw, Mapping):
        raise ValueError("engineering obligation decision must be an object")
    decision = _json_copy(dict(raw))
    if set(decision) != _DECISION_FIELDS:
        raise ValueError("engineering obligation decision fields are invalid")
    kind = decision["obligation_kind"]
    outcomes = OBLIGATION_OUTCOMES.get(kind)
    if outcomes is None or decision["outcome"] not in outcomes:
        raise ValueError("engineering obligation kind/outcome is invalid")
    if decision["resolution_level"] not in RESOLUTION_LEVELS:
        raise ValueError("engineering obligation resolution_level is invalid")
    decision["rationale"] = _nonblank(decision["rationale"], "decision rationale")
    evidence_refs = decision["evidence_refs"]
    if not isinstance(evidence_refs, list) or any(
        not isinstance(value, str) or not value.strip() for value in evidence_refs
    ):
        raise ValueError("decision evidence_refs must be strings")
    if len(evidence_refs) != len(set(evidence_refs)):
        raise ValueError("decision evidence_refs must be unique")

    if kind == "standard_parts_assessment":
        triggers = standard_part_triggers(scope)
        if decision["outcome"] in {"not_applicable", "no_candidates"} and triggers:
            raise ValueError(
                "standard-parts screening conclusion conflicts with scope triggers: "
                + ", ".join(triggers)
            )
        if decision["outcome"] in {
            "candidates_resolved",
            "approved_custom_exception",
        } and not triggers:
            raise ValueError("expanded standard-parts conclusion requires scope triggers")
        if decision["outcome"] in {
            "candidates_resolved",
            "approved_custom_exception",
        } and not evidence_refs:
            raise ValueError("expanded standard-parts conclusion requires evidence")
    else:
        triggers = assembly_triggers(scope)
        if decision["outcome"] == "not_applicable" and triggers:
            raise ValueError(
                "assembly not_applicable conflicts with scope triggers: "
                + ", ".join(triggers)
            )
        if decision["outcome"] in {"required_pending", "required_passed"} and not triggers:
            raise ValueError("expanded assembly conclusion requires assembly triggers")
        if decision["outcome"] == "required_passed" and not evidence_refs:
            raise ValueError("passed assembly conclusion requires validation evidence")
    return decision


def build_obligation_read_model(
    *,
    scope: Mapping[str, Any] | None,
    family_outcome: str | None,
    knowledge_outcome: str | None,
    standard_parts_decision: Mapping[str, Any] | None,
    assembly_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Render independent obligation conclusions without imposing an order."""

    scope_hash = engineering_scope_sha256(scope) if scope is not None else None
    resolved: list[dict[str, Any]] = []
    open_items: list[dict[str, Any]] = []
    recommended: list[str] = []

    if family_outcome in {"matched", "no_match", "not_configured"}:
        resolved.append({"obligation_kind": "product_family_resolution", "outcome": family_outcome})
    else:
        open_items.append({"obligation_kind": "product_family_resolution"})
        recommended.append("product_family_match")

    if knowledge_outcome in {"completed_matches", "completed_no_matches"}:
        resolved.append({"obligation_kind": "knowledge_retrieval", "outcome": knowledge_outcome})
    else:
        open_items.append({"obligation_kind": "knowledge_retrieval"})
        recommended.append("design_knowledge_retrieve")

    for kind, decision in (
        ("standard_parts_assessment", standard_parts_decision),
        ("assembly_assessment", assembly_decision),
    ):
        if decision is not None and scope is not None:
            checked = validate_obligation_decision(decision, scope)
            resolved.append({**deepcopy(checked), "scope_sha256": scope_hash})
        else:
            open_items.append({"obligation_kind": kind})
            recommended.append("design_job_obligations_resolve")

    mutation_ready = not open_items
    allowed_actions = list(dict.fromkeys(recommended))
    if scope is None:
        allowed_actions.append("prepare_design_intent")
    if mutation_ready:
        allowed_actions.append("design_change_mutation_authorize")
    return {
        "schema_version": "EngineeringObligationReadModel/v1",
        "scope_sha256": scope_hash,
        "open_obligations": open_items,
        "resolved_obligations": resolved,
        "recommended_actions": list(dict.fromkeys(recommended)),
        "allowed_actions": allowed_actions,
        "blocked_actions": (
            {}
            if mutation_ready
            else {
                "design_change_mutation_authorize": [
                    item["obligation_kind"] for item in open_items
                ]
            }
        ),
    }


def require_obligation_gate(
    *,
    scope: Mapping[str, Any],
    family_outcome: str | None,
    knowledge_outcome: str | None,
    standard_parts_decision: Mapping[str, Any] | None,
    assembly_decision: Mapping[str, Any] | None,
    for_delivery: bool = False,
) -> None:
    """Fail closed for the operation's prerequisites, without imposing an order."""

    validated_scope = validate_engineering_scope(scope)
    if family_outcome not in {"matched", "no_match", "not_configured"}:
        raise ValueError("product-family resolution must reach a terminal conclusion")
    if knowledge_outcome not in {"completed_matches", "completed_no_matches"}:
        raise ValueError("knowledge retrieval must be completed before CAD mutation")
    if standard_parts_decision is None:
        raise ValueError("standard-parts assessment must be resolved for the approved scope")
    validate_obligation_decision(standard_parts_decision, validated_scope)
    if assembly_decision is None:
        raise ValueError("assembly assessment must be resolved for the approved scope")
    assembly = validate_obligation_decision(assembly_decision, validated_scope)
    if for_delivery and assembly_triggers(validated_scope):
        if assembly["outcome"] != "required_passed":
            raise ValueError(
                "delivery requires a passed assembly assessment for the approved scope"
            )
