from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Mapping


class ApprovalBoundaryError(RuntimeError):
    """Raised when a CAD mutation has no unambiguous approval authority."""


_ENVELOPE_REQUIRED_FIELDS = (
    "design_intent",
    "architecture",
    "key_interfaces",
    "user_constraints",
    "manufacturing_method",
    "material_constraints",
    "validation_requirements",
)

_IMPACT_REQUIRED_FIELDS = (
    "change_kind",
    "mechanism_changed",
    "architecture_changed",
    "key_interfaces_changed",
    "functional_change",
    "constraint_impacts",
    "manufacturing_process_changed",
    "material_constraints_changed",
    "standard_part_categories_added",
    "validation_requirements_removed",
    "boundary_certainty",
)

_AUTONOMOUS_CHANGE_KINDS = frozenset(
    {
        "parameter_optimization",
        "feature_detail",
        "clearance_adjustment",
        "interference_repair",
        "geometry_validity_repair",
        "validation_repair",
        "implementation_refinement",
    }
)


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _is_nonempty_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and bool(value)


def _is_list(value: object) -> bool:
    return isinstance(value, list)


def validate_approval_envelope_draft(draft: Mapping[str, Any]) -> None:
    """Validate the governed sections needed to approve a design intent."""

    if not isinstance(draft, Mapping):
        raise ValueError("approval envelope draft must be an object")

    for field in _ENVELOPE_REQUIRED_FIELDS:
        if field not in draft:
            raise ValueError(f"approval envelope draft is missing {field}")

    for field in ("design_intent", "architecture", "manufacturing_method"):
        if not _is_nonempty_mapping(draft[field]):
            raise ValueError(f"approval envelope {field} must be a non-empty object")

    for field in (
        "key_interfaces",
        "user_constraints",
        "material_constraints",
        "validation_requirements",
    ):
        if not _is_list(draft[field]):
            raise ValueError(f"approval envelope {field} must be a list")

    if not draft["validation_requirements"]:
        raise ValueError("approval envelope validation_requirements must not be empty")
    for field in (
        "key_interfaces",
        "user_constraints",
        "material_constraints",
        "validation_requirements",
    ):
        for item in draft[field]:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
            ):
                raise ValueError(
                    f"approval envelope {field} entries require a nonblank id"
                )


def build_approval_envelope(
    *,
    envelope_id: str,
    approval_change_set_id: str,
    job_id: str,
    working_copy_id: str,
    draft: Mapping[str, Any],
    approval_actor: str,
    approval_text: str,
    approval_time: datetime,
    approval_revision: int,
    envelope_revision: int,
) -> dict[str, Any]:
    """Build an immutable, active approval envelope from an approved draft."""

    validate_approval_envelope_draft(draft)
    if approval_revision < 1 or envelope_revision < 1:
        raise ValueError("approval and envelope revisions must be positive")

    envelope: dict[str, Any] = {
        "id": envelope_id,
        "approval_change_set_id": approval_change_set_id,
        "job_id": job_id,
        "working_copy_id": working_copy_id,
        **deepcopy(dict(draft)),
        "approved_by": approval_actor,
        "approval_text": approval_text,
        "approved_at": _iso_timestamp(approval_time),
        "approval_revision": approval_revision,
        "envelope_revision": envelope_revision,
        "status": "active",
    }
    return envelope


def classify_change_against_envelope(
    envelope: Mapping[str, Any] | None,
    semantic_impact: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify an implementation change using semantic approval boundaries.

    The classifier deliberately ignores percentage-only materiality hints. A
    change is autonomous only when every declared semantic boundary is known
    to remain inside the active envelope.
    """

    reasons: list[str] = []
    if not envelope or envelope.get("status") != "active":
        reasons.append("no_active_approval_envelope")

    if not _valid_semantic_impact(semantic_impact):
        reasons.append("incomplete_semantic_impact")
        return _classification(reasons)

    if semantic_impact["change_kind"] not in _AUTONOMOUS_CHANGE_KINDS:
        reasons.append("change_kind_not_autonomously_authorized")
    if semantic_impact["mechanism_changed"] is not False:
        reasons.append("mechanism_changed")
    if semantic_impact["architecture_changed"] is not False:
        reasons.append("architecture_changed")
    if semantic_impact["key_interfaces_changed"]:
        reasons.append("key_interface_changed")
    if semantic_impact["functional_change"] not in (None, "none", "unchanged"):
        reasons.append("approved_function_changed")
    if semantic_impact["manufacturing_process_changed"] is not False:
        reasons.append("manufacturing_process_changed")
    if semantic_impact["material_constraints_changed"] is not False:
        reasons.append("material_constraints_changed")
    if semantic_impact["standard_part_categories_added"]:
        reasons.append("standard_part_category_added")
    if semantic_impact["validation_requirements_removed"]:
        reasons.append("validation_requirement_removed")

    impacts = semantic_impact["constraint_impacts"]
    expected_constraint_ids = {
        item["id"]
        for item in (envelope or {}).get("user_constraints", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    actual_constraint_ids = {item["constraint_id"] for item in impacts}
    if actual_constraint_ids != expected_constraint_ids:
        reasons.append("incomplete_semantic_impact")
    outcomes = {item["outcome"] for item in impacts}
    if "exceeds" in outcomes:
        reasons.append("approved_constraint_exceeded")
    if outcomes.intersection({"unknown", "ambiguous", "outside"}):
        reasons.append("boundary_not_reliably_inside")

    if semantic_impact["boundary_certainty"] != "inside":
        reasons.append("boundary_not_reliably_inside")

    return _classification(reasons)


def _valid_semantic_impact(value: object) -> bool:
    if not isinstance(value, Mapping) or any(
        field not in value for field in _IMPACT_REQUIRED_FIELDS
    ):
        return False
    if not isinstance(value["change_kind"], str):
        return False
    for field in (
        "mechanism_changed",
        "architecture_changed",
        "manufacturing_process_changed",
        "material_constraints_changed",
    ):
        if not isinstance(value[field], bool):
            return False
    for field in (
        "key_interfaces_changed",
        "constraint_impacts",
        "standard_part_categories_added",
        "validation_requirements_removed",
    ):
        if not isinstance(value[field], list):
            return False
    if value["functional_change"] is not None and not isinstance(
        value["functional_change"], str
    ):
        return False
    if not isinstance(value["boundary_certainty"], str):
        return False
    seen_constraints: set[str] = set()
    for item in value["constraint_impacts"]:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("constraint_id"), str)
            or not item["constraint_id"].strip()
            or item.get("outcome") not in {"within", "exceeds", "unknown"}
            or item["constraint_id"] in seen_constraints
        ):
            return False
        seen_constraints.add(item["constraint_id"])
    return True


def _classification(reasons: list[str]) -> dict[str, Any]:
    unique_reasons = list(dict.fromkeys(reasons))
    requires_approval = bool(unique_reasons)
    return {
        "status": (
            "requires_human_approval" if requires_approval else "within_envelope"
        ),
        "requires_human_approval": requires_approval,
        "reasons": unique_reasons,
        "rule_basis": "semantic_design_intent",
    }


def require_mutation_authorization(
    change_set: Mapping[str, Any],
    *,
    active_envelope_id: str | None,
) -> None:
    """Fail closed unless a change is approved under an active envelope."""

    envelope_id = change_set.get("approval_envelope_id")
    authorized_mode = change_set.get("authorization_mode") in {
        "human_approval",
        "approval_envelope",
    }
    if (
        change_set.get("status") != "approved"
        or not envelope_id
        or envelope_id != active_envelope_id
        or not authorized_mode
    ):
        raise ApprovalBoundaryError(
            "CAD mutation requires an active approved envelope bound to the change set"
        )


def build_change_audit_event(
    *,
    event_id: str,
    change_set_id: str,
    envelope_id: str | None,
    event_type: str,
    actor_id: str,
    decision: Mapping[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    """Build an append-only audit event for approval-envelope decisions."""

    return {
        "id": event_id,
        "change_set_id": change_set_id,
        "approval_envelope_id": envelope_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "decision": deepcopy(dict(decision)),
        "created_at": _iso_timestamp(created_at),
    }
