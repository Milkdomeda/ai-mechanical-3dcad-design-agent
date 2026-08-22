from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .hashing import stable_hash
from .models import AssertionProposal


GENERIC_NAME = re.compile(r"^(part|body|feature|solid|component|object|unnamed|shape)[ _.-]*\d*$", re.IGNORECASE)


def _score(uncertainty: float, recurrence: float, centrality: float, design_impact: float, blocking: bool = False) -> float:
    if blocking:
        return 100.0 + uncertainty
    return round(100.0 * (0.35 * uncertainty + 0.25 * recurrence + 0.20 * centrality + 0.20 * design_impact), 6)


def generate_question_targets(
    manifest: dict[str, Any],
    *,
    family_confirmed: bool,
    limit: int = 5,
    excluded_signatures: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 5:
        raise ValueError("question target limit must be between 1 and 5")
    candidates: list[dict[str, Any]] = []
    if not family_confirmed:
        candidates.append(
            {
                "question_kind": "product-family-identity",
                "target_refs": ["model:root"],
                "evidence": [{"source": manifest.get("source", {}), "document": manifest.get("document", {})}],
                "score": _score(1.0, 1.0, 1.0, 1.0, blocking=True),
                "prompt_intent": "Confirm the product identity, product family, purpose, and whether a new family is required.",
            }
        )
    shape_nodes = manifest.get("shape_definitions", [])
    count = max(1, len(shape_nodes))
    for node in shape_nodes:
        if node.get("node_kind") == "multi-solid-shape" or node.get("topology", {}).get("solid_count", 0) > 1:
            candidates.append(
                {
                    "question_kind": "multi-solid-structure",
                    "target_refs": [node["source_id"]],
                    "evidence": [
                        {
                            "label": node.get("source_label"),
                            "solid_count": node.get("topology", {}).get("solid_count"),
                            "bbox_mm": node.get("bbox_mm"),
                        }
                    ],
                    "score": _score(0.95, 0.2, 0.8, 0.9, blocking=True),
                    "prompt_intent": "Determine whether disconnected solids are one manufactured part, a subassembly, or export fragments.",
                }
            )
        label = str(node.get("source_label") or node.get("source_name") or "")
        if not label or GENERIC_NAME.fullmatch(label.strip()):
            candidates.append(
                {
                    "question_kind": "canonical-component-name",
                    "target_refs": [node["source_id"]],
                    "evidence": [{"source_name": node.get("source_name"), "bbox_mm": node.get("bbox_mm")}],
                    "score": _score(0.95, 1.0 / count, 0.5, 0.65),
                    "prompt_intent": "Capture the engineer-approved canonical name and aliases without inferring from geometry alone.",
                }
            )
    for group in manifest.get("repeated_shape_groups", []):
        recurrence = min(1.0, float(group["count"]) / count)
        candidates.append(
            {
                "question_kind": "repeated-shape-identity",
                "target_refs": list(group["source_ids"]),
                "evidence": [group],
                "score": _score(0.65, recurrence, 0.6, 0.75),
                "prompt_intent": "Confirm whether identical geometry instances share one part definition; do not assume identical function.",
            }
        )
    relation_priority = {
        "interference": 1.0,
        "bbox-overlap": 0.85,
        "contact": 0.8,
        "clearance": 0.75,
        "coaxial": 0.7,
        "coplanar": 0.6,
        "contains": 0.5,
        "contained-by": 0.5,
    }
    for relation in manifest.get("relation_candidates", []):
        kinds = relation.get("candidates", [])
        if not kinds:
            continue
        design_impact = max(relation_priority.get(kind, 0.35) for kind in kinds)
        candidates.append(
            {
                "question_kind": "interface-or-spatial-relation",
                "target_refs": [relation["subject_source_id"], relation["object_source_id"]],
                "evidence": [relation],
                "score": _score(0.7, 0.2, min(1.0, len(kinds) / 2.0), design_impact, blocking="interference" in kinds),
                "prompt_intent": "Confirm whether the candidate relation is an intended engineering interface, allowed contact, or export artifact.",
            }
        )
    roots = [
        node
        for node in manifest.get("source_nodes", shape_nodes)
        if node.get("has_shape") and not node.get("primary_parent_source_id")
    ]
    if len(roots) > 1:
        candidates.append(
            {
                "question_kind": "top-level-product-boundary",
                "target_refs": [node["source_id"] for node in roots[:50]],
                "evidence": [{"root_count": len(roots), "root_labels": [node.get("source_label") for node in roots[:50]]}],
                "score": _score(0.9, 0.5, 1.0, 0.9, blocking=True),
                "prompt_intent": "Confirm whether the file contains one product, several products, or a flattened assembly.",
            }
        )
    # Stable de-duplication keeps the highest score for each question kind/target set.
    best: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = stable_hash([candidate["question_kind"], sorted(candidate["target_refs"])])
        if key not in best or candidate["score"] > best[key]["score"]:
            best[key] = candidate
    excluded = excluded_signatures or set()
    remaining = [item for key, item in best.items() if key not in excluded]
    return sorted(remaining, key=lambda item: (-item["score"], item["question_kind"]))[:limit]


def parse_assertion_proposals(raw: list[dict[str, Any]]) -> list[AssertionProposal]:
    if not raw:
        raise ValueError("at least one assertion proposal is required")
    proposals = []
    for item in raw:
        proposal = AssertionProposal(
            subject_ref=str(item.get("subject_ref", "")),
            predicate=str(item.get("predicate", "")),
            object_value=item.get("object_value"),
            scope_kind=str(item.get("scope_kind", "model")),
            risk_level=str(item.get("risk_level", "R1")),
            status=str(item.get("status", "inferred_candidate")),
            unit=str(item.get("unit", "")),
            confidence=float(item.get("confidence", 0.5)),
            evidence=list(item.get("evidence", [])),
            applicability=dict(item.get("applicability", {})),
            non_applicable_conditions=list(item.get("non_applicable_conditions", [])),
            contradicts=list(item.get("contradicts", [])),
            supersedes=str(item.get("supersedes", "")),
            source_kind=str(item.get("source_kind", "codex_interpretation")),
        )
        proposals.append(proposal.validate())
    return proposals


def family_statistics(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    if not manifests:
        return {
            "schema_version": "FamilyComparison/v1",
            "distinct_model_count": 0,
            "statistics": {},
            "generalization_allowed": False,
        }
    part_counts = [len(item.get("shape_definitions", [])) for item in manifests]
    solid_counts = [len(item.get("solid_fragments", [])) for item in manifests]
    repeated_counts = [sum(group.get("count", 0) for group in item.get("repeated_shape_groups", [])) for item in manifests]
    relation_presence = Counter()
    surface_presence = Counter()
    dimension_rows = []
    for manifest in manifests:
        kinds = set()
        for relation in manifest.get("relation_candidates", []):
            kinds.update(relation.get("candidates", []))
        relation_presence.update(kinds)
        surface_types = set()
        for shape in manifest.get("shape_definitions", []):
            surface_types.update(shape.get("surface_type_counts", {}).keys())
            size = shape.get("bbox_mm", {}).get("size", [])
            if len(size) == 3:
                dimension_rows.append([float(value) for value in size])
        surface_presence.update(surface_types)
    model_count = len(manifests)
    dimension_range = {}
    if dimension_rows:
        dimension_range = {
            "minimum_mm": [min(row[index] for row in dimension_rows) for index in range(3)],
            "maximum_mm": [max(row[index] for row in dimension_rows) for index in range(3)],
        }
    return {
        "schema_version": "FamilyComparison/v1",
        "distinct_model_count": model_count,
        "generalization_allowed": model_count >= 3,
        "statistics": {
            "part_count_range": [min(part_counts), max(part_counts)],
            "solid_fragment_count_range": [min(solid_counts), max(solid_counts)],
            "repeated_instance_count_range": [min(repeated_counts), max(repeated_counts)],
            "relation_prevalence": {key: {"support": value, "total": model_count} for key, value in sorted(relation_presence.items())},
            "surface_type_prevalence": {key: {"support": value, "total": model_count} for key, value in sorted(surface_presence.items())},
            "component_dimension_range": dimension_range,
        },
        "warning": (
            "Fewer than three distinct models: statistics are project observations, not family rules."
            if model_count < 3
            else "Statistics may be proposed for engineer review; no rule is auto-approved."
        ),
    }
