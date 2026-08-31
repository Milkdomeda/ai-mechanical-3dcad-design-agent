from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any


def _load_fastener_model_evidence(
    manifest: dict[str, Any],
) -> tuple[set[str], list[str]]:
    """Load one authoritative, same-revision FreeCAD fastener inventory."""
    expected_sha = str(manifest.get("model_sha256", ""))
    inventories: list[set[str]] = []
    failures: list[str] = []
    evidence_items = manifest.get("fastener_geometry_checks", [])
    if not isinstance(evidence_items, list):
        evidence_items = []
        failures.append("invalid-fastener-evidence-list")

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            failures.append("invalid-fastener-evidence-object")
            continue
        evidence_id = str(evidence.get("id", "")) or "unnamed-fastener-evidence"
        report_path = Path(str(evidence.get("report_path", ""))).expanduser()
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append(evidence_id)
            continue
        if not isinstance(report, dict):
            failures.append(evidence_id)
            continue

        inventory = report.get("fastener_inventory")
        inventory_items = inventory if isinstance(inventory, list) else []
        valid_inventory_items = [
            item
            for item in inventory_items
            if isinstance(item, dict)
            and str(item.get("object_id", ""))
            and str(item.get("contract_check_id", ""))
        ]
        object_ids = {str(item["object_id"]) for item in valid_inventory_items}
        report_checks = report.get("checks")
        check_items = report_checks if isinstance(report_checks, list) else []
        valid_check_items = [item for item in check_items if isinstance(item, dict)]
        check_map = {
            str(item.get("id", "")): item
            for item in valid_check_items
            if str(item.get("id", ""))
        }
        summary = report.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        contracts_pass = (
            isinstance(inventory, list)
            and len(valid_inventory_items) == len(inventory_items)
            and len(object_ids) == len(inventory_items)
            and all(
                check_map.get(str(item["contract_check_id"]), {}).get("status") == "passed"
                and check_map.get(str(item["contract_check_id"]), {}).get("mandatory") is True
                for item in valid_inventory_items
            )
        )
        fastener_checks_pass = all(
            item.get("status") == "passed"
            for item in valid_check_items
            if item.get("mandatory", True)
            and (
                str(item.get("id", "")).startswith("fastener.")
                or str(item.get("id", "")).startswith("fastener-set.")
            )
        )
        check_contract_pass = (
            isinstance(report_checks, list)
            and len(valid_check_items) == len(check_items)
            and len(check_map) == len(check_items)
            and all(
                item.get("validator") == "freecad-model-validation"
                for item in valid_check_items
            )
        )
        coverage = check_map.get("fastener.coverage", {})
        valid = (
            evidence.get("status") == "passed"
            and evidence.get("validator") == "freecad-model-validation"
            and evidence.get("model_sha256") == expected_sha
            and report.get("status") == "passed"
            and report.get("validator") == "freecad-model-validation"
            and report.get("kind") == "FCStd"
            and report.get("model_sha256") == expected_sha
            and summary.get("fasteners_detected") == len(object_ids)
            and coverage.get("mandatory") is True
            and coverage.get("status") == "passed"
            and coverage.get("actual") == len(object_ids)
            and contracts_pass
            and fastener_checks_pass
            and check_contract_pass
        )
        if valid:
            inventories.append(object_ids)
        else:
            failures.append(evidence_id)

    if len(inventories) != 1:
        failures.append("authoritative-fastener-inventory-count")
    return (inventories[0] if len(inventories) == 1 else set(), sorted(set(failures)))


def validate_assembly_completeness(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate BOM and physical connection completeness without semantic inference."""
    checks: list[dict[str, Any]] = []

    def check(check_id: str, passed: bool, message: str, evidence: Any = None) -> None:
        item: dict[str, Any] = {
            "id": check_id,
            "validator": "assembly-completeness-validation",
            "mandatory": True,
            "status": "passed" if passed else "failed",
            "message": message,
        }
        if evidence is not None:
            item["evidence"] = evidence
        checks.append(item)

    check(
        "manifest.schema",
        manifest.get("schema_version") == "AssemblyCompleteness/v2",
        "manifest uses AssemblyCompleteness/v2",
        manifest.get("schema_version"),
    )
    components = manifest.get("components") if isinstance(manifest.get("components"), list) else []
    bom = manifest.get("bom") if isinstance(manifest.get("bom"), list) else []
    joints = manifest.get("joints") if isinstance(manifest.get("joints"), list) else []
    component_ids = [str(item.get("id", "")) for item in components if isinstance(item, dict)]
    bom_ids = [str(item.get("id", "")) for item in bom if isinstance(item, dict)]
    joint_ids = [str(item.get("id", "")) for item in joints if isinstance(item, dict)]
    check(
        "identity.unique",
        all(component_ids) and len(component_ids) == len(set(component_ids))
        and all(bom_ids) and len(bom_ids) == len(set(bom_ids))
        and all(joint_ids) and len(joint_ids) == len(set(joint_ids)),
        "component, BOM, and joint IDs are non-empty and unique",
    )
    component_map = {str(item.get("id")): item for item in components if isinstance(item, dict) and item.get("id")}
    bom_map = {str(item.get("id")): item for item in bom if isinstance(item, dict) and item.get("id")}
    in_scope = {item_id for item_id, item in component_map.items() if item.get("in_scope", True)}
    ground = str(manifest.get("ground_component_id", ""))
    check("ground.present", ground in in_scope, "ground component exists and is in scope", ground)

    bad_quantities = [item_id for item_id, item in bom_map.items() if not isinstance(item.get("quantity"), int) or item["quantity"] <= 0]
    check("bom.quantities", not bad_quantities, "all BOM quantities are positive integers", bad_quantities)
    missing_bom = [item_id for item_id in sorted(in_scope) if str(component_map[item_id].get("bom_item_id", "")) not in bom_map]
    check("bom.coverage", not missing_bom, "every in-scope component is represented by a BOM item", missing_bom)

    envelope_only = [item_id for item_id in sorted(in_scope) if component_map[item_id].get("interface_level") == "envelope_only"]
    check("placeholder.no-envelope-only", not envelope_only, "no in-scope component is only an exterior envelope", envelope_only)
    incomplete_placeholders: list[str] = []
    for item_id in sorted(in_scope):
        item = component_map[item_id]
        if not item.get("placeholder"):
            continue
        contract = item.get("interface_contract") if isinstance(item.get("interface_contract"), dict) else {}
        required = ("mating_faces", "mounting_features", "datum_frames")
        if any(not isinstance(contract.get(field), list) or not contract[field] for field in required):
            incomplete_placeholders.append(item_id)
    check(
        "placeholder.interface-contract",
        not incomplete_placeholders,
        "placeholders retain mating faces, mounting features, and datum frames",
        incomplete_placeholders,
    )

    adjacency: dict[str, set[str]] = defaultdict(set)
    invalid_joints: list[str] = []
    incomplete_fastened_joints: list[str] = []
    for joint in joints:
        if not isinstance(joint, dict):
            continue
        joint_id = str(joint.get("id", ""))
        a, b = str(joint.get("a", "")), str(joint.get("b", ""))
        if a not in component_map or b not in component_map or not joint.get("interface_a") or not joint.get("interface_b"):
            invalid_joints.append(joint_id)
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)
        if joint.get("kind") in {"fastened", "clamped", "bolted"}:
            fastener_ids = joint.get("fastener_bom_item_ids")
            fastener_object_ids = joint.get("fastener_object_ids")
            quantity = joint.get("fastener_quantity")
            if (
                not isinstance(fastener_ids, list)
                or not fastener_ids
                or any(str(item_id) not in bom_map for item_id in fastener_ids)
                or not isinstance(fastener_object_ids, list)
                or not fastener_object_ids
                or any(not str(object_id) for object_id in fastener_object_ids)
                or not isinstance(quantity, int)
                or quantity <= 0
                or quantity != len(fastener_object_ids)
            ):
                incomplete_fastened_joints.append(joint_id)
    check("joints.references", not invalid_joints, "all joints reference modeled components and interfaces", invalid_joints)
    check(
        "joints.fasteners",
        not incomplete_fastened_joints,
        "every fastened joint references fastener BOM items and an equal quantity of fastener objects",
        incomplete_fastened_joints,
    )
    detected_fasteners, invalid_fastener_evidence = _load_fastener_model_evidence(manifest)
    check(
        "joints.fastener-model-evidence",
        not invalid_fastener_evidence,
        "one passed same-revision model report inventories every detected fastener",
        invalid_fastener_evidence,
    )
    assigned = [
        str(object_id)
        for joint in joints
        if isinstance(joint, dict) and joint.get("kind") in {"fastened", "clamped", "bolted"}
        for object_id in (
            joint.get("fastener_object_ids", [])
            if isinstance(joint.get("fastener_object_ids"), list)
            else []
        )
    ]
    counts = Counter(assigned)
    duplicates = sorted(item for item, count in counts.items() if count > 1)
    missing = sorted(detected_fasteners - set(assigned))
    unknown = sorted(set(assigned) - detected_fasteners)
    check(
        "joints.fastener-occurrence-coverage",
        not duplicates and not missing and not unknown,
        "every model-detected fastener is assigned exactly once to a fastened joint",
        {"duplicates": duplicates, "missing": missing, "unknown": unknown},
    )

    critical_interface_ids = {
        str(item.get("id"))
        for item in manifest.get("critical_interfaces", [])
        if isinstance(item, dict) and item.get("id")
    }
    covered_critical_interfaces: set[str] = set()
    invalid_mechanical_evidence: list[str] = []
    for evidence in manifest.get("mechanical_interface_checks", []):
        if not isinstance(evidence, dict):
            invalid_mechanical_evidence.append("")
            continue
        evidence_id = str(evidence.get("id", ""))
        report_path = Path(str(evidence.get("report_path", ""))).expanduser()
        try:
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            report = {}
        passed_report_ids = {
            str(interface.get("id"))
            for interface in report.get("interfaces", [])
            if isinstance(interface, dict) and interface.get("id") and interface.get("status") == "passed"
        }
        evidence_interface_ids = {str(item) for item in evidence.get("interface_ids", [])}
        if (
            evidence.get("status") != "passed"
            or evidence.get("validator") != "freecad-mechanical-interface-validation"
            or evidence.get("model_sha256") != manifest.get("model_sha256")
            or report.get("status") != "passed"
            or report.get("validator") != "freecad-mechanical-interface-validation"
            or report.get("model_sha256") != manifest.get("model_sha256")
            or not evidence_interface_ids
            or not evidence_interface_ids.issubset(passed_report_ids)
            or not evidence_interface_ids.issubset(critical_interface_ids)
        ):
            invalid_mechanical_evidence.append(evidence_id)
            continue
        covered_critical_interfaces.update(evidence_interface_ids)
    missing_mechanical_geometry = sorted(critical_interface_ids - covered_critical_interfaces)
    check(
        "interfaces.mechanical-geometry-evidence",
        not invalid_mechanical_evidence and not missing_mechanical_geometry,
        "every declared critical mechanical interface has passed same-revision axis/contact/clearance evidence",
        {"invalid_evidence": invalid_mechanical_evidence, "uncovered_interface_ids": missing_mechanical_geometry},
    )

    visited: set[str] = set()
    if ground in component_map:
        queue: deque[str] = deque([ground])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            queue.extend(adjacency[node] - visited)
    orphans = sorted(in_scope - visited)
    check("assembly.connected", not orphans, "all in-scope components are connected to ground", orphans)

    bad_paths: list[str] = []
    for path in manifest.get("functional_load_paths", []):
        if not isinstance(path, dict):
            continue
        nodes = [str(node) for node in path.get("components", [])]
        if not nodes or nodes[-1] != ground or any(b not in adjacency[a] for a, b in zip(nodes, nodes[1:])):
            bad_paths.append(str(path.get("id", "")))
    check("load-paths.connected", not bad_paths, "declared functional load paths are joint-connected to ground", bad_paths)

    moving = {item_id for item_id, item in component_map.items() if item.get("role") in {"moving", "motion_transfer"}}
    passed_motion = {
        str(item.get("component_id"))
        for item in manifest.get("motion_checks", [])
        if isinstance(item, dict) and item.get("status") == "passed" and item.get("evidence")
    }
    unchecked_motion = sorted(moving - passed_motion)
    check("motion.verified", not unchecked_motion, "every moving or motion-transfer component has evidenced motion clearance", unchecked_motion)

    bad_boundaries = [
        str(item.get("id", ""))
        for item in manifest.get("design_boundary", [])
        if not isinstance(item, dict) or not item.get("interface") or not item.get("owner") or not item.get("status")
    ]
    check("boundary.explicit", not bad_boundaries, "external interfaces have an explicit owner and status", bad_boundaries)

    status = "passed" if checks and all(item["status"] == "passed" for item in checks) else "failed"
    return {
        "schema_version": "AssemblyCompletenessValidation/v2",
        "status": status,
        "summary": {
            "components": len(components),
            "bom_items": len(bom),
            "joints": len(joints),
            "mandatory_failures": sum(item["status"] != "passed" for item in checks),
        },
        "checks": checks,
    }
