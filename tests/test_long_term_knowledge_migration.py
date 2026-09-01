from __future__ import annotations

from copy import deepcopy

import pytest

from mechanical_design_agent.long_term_knowledge_migration import (
    ALLOWED_SOURCE_KEYS,
    build_long_term_export,
    build_parity_probes,
)
from mechanical_design_agent.models import canonical_json


def _source() -> dict[str, list[dict[str, object]]]:
    assertions: list[dict[str, object]] = []
    documents: list[dict[str, object]] = []
    for index in range(43):
        assertion_id = f"assertion-{index:02d}"
        family_id = "PF-PILOT-001" if index < 22 else "horizontal-vacuum-vessel"
        assertions.append(
            {
                "id": assertion_id,
                "organization_id": "org-001",
                "design_group_id": "group-001",
                "family_id": family_id,
                "subject_ref": f"subject-{index}",
                "predicate": "has_engineering_fact",
                "object_value": {"value": index},
                "applicability": {"design_type": "carrier"},
                "non_applicable_conditions": [],
                "evidence": [{"source": f"evidence-{index}"}],
                "status": "approved",
                "supersedes": None,
                "source_kind": "engineer_confirmed",
                "risk_level": "normal",
                "confidence": 0.9,
                "created_by": "engineer",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        documents.append(
            {
                "assertion_id": assertion_id,
                "family_id": family_id,
                "exact_terms": ["guide rail"] if index == 0 else [f"term-{index}"],
                "search_text": f"engineering fact {index}",
            }
        )
    lessons = []
    for index in range(4):
        lessons.append(
            {
                "id": f"event-{index}",
                "lesson_key": f"lesson-key-{index}",
                "revision": 1,
                "organization_id": "org-001",
                "source_design_group_id": "group-001",
                "source_family_id": "PF-PILOT-001" if index == 0 else None,
                "title": f"Lesson {index}",
                "problem": f"Problem {index}",
                "root_causes": [f"Cause {index}"],
                "corrections": [f"Correction {index}"],
                "prevention": [f"Prevention {index}"],
                "applicability": {"summary": "printed carriers"},
                "non_applicable_conditions": ["metal welded carrier"],
                "search_terms": [f"lesson-term-{index}"],
                "evidence_manifest": [{"path": f"report-{index}.json"}],
                "status": "approved",
                "supersedes": None,
                "approved_by": "engineer",
                "approval_text": "approved",
                "approved_at": "2026-01-02T00:00:00+00:00",
            }
        )
    return {
        "organizations": [{"id": "org-001", "name": "Organization"}],
        "design_groups": [
            {"id": "group-001", "organization_id": "org-001", "name": "Group"}
        ],
        "product_families": [
            {
                "id": "PF-PILOT-001",
                "organization_id": "org-001",
                "design_group_id": "group-001",
                "canonical_name": "Pilot Product Family",
                "aliases": ["pilot-family", "PF pilot"],
                "status": "learning-in-progress",
                "config": {"design_policy": {"approval_before_delivery": True}},
                "revision": 8,
            },
            {
                "id": "horizontal-vacuum-vessel",
                "organization_id": "org-001",
                "design_group_id": "group-001",
                "canonical_name": "Horizontal Vacuum Vessel",
                "aliases": ["vacuum vessel"],
                "status": "ready-for-manual-ingest",
                "config": {},
                "revision": 4,
            },
        ],
        "family_profiles": [
            {
                "id": "proposed-profile",
                "family_id": "PF-PILOT-001",
                "revision": 1,
                "status": "proposed",
                "profile": {"mechanism_description": "unapproved"},
                "evidence": [],
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "approved-profile",
                "family_id": "PF-PILOT-001",
                "revision": 2,
                "status": "approved",
                "profile": {"mechanism_description": "guided linear mechanism"},
                "evidence": [{"source": "profile-evidence"}],
                "created_at": "2026-01-02T00:00:00+00:00",
            },
            {
                "id": "rejected-profile",
                "family_id": "PF-PILOT-001",
                "revision": 3,
                "status": "rejected",
                "profile": {"mechanism_description": "rejected"},
                "evidence": [],
                "created_at": "2026-01-03T00:00:00+00:00",
            },
        ],
        "knowledge_assertions": assertions,
        "knowledge_search_documents": documents,
        "design_lesson_events": lessons,
    }


def test_source_keys_are_restricted_to_long_term_knowledge() -> None:
    assert ALLOWED_SOURCE_KEYS == frozenset(_source())


def test_export_contains_only_approved_long_term_knowledge() -> None:
    export = build_long_term_export(_source())

    assert [row["id"] for row in export.product_families] == [
        "PF-PILOT-001",
        "horizontal-vacuum-vessel",
    ]
    assert len(export.knowledge_assertions) == 43
    assert len(export.design_lessons) == 4
    serialized = canonical_json(export.as_dict())
    assert "proposed-profile" not in serialized
    assert "rejected-profile" not in serialized
    assert "approval_before_delivery" not in serialized


def test_export_preserves_family_matching_inputs() -> None:
    export = build_long_term_export(_source())
    family = next(
        row for row in export.product_families if row["id"] == "PF-PILOT-001"
    )

    assert family["canonical_name"] == "Pilot Product Family"
    assert family["aliases"] == ["PF pilot", "pilot-family"]
    assert "guide rail" in family["knowledge"]["retrieval_terms"]
    assert (
        family["knowledge"]["approved_profile"]["mechanism_description"]
        == "guided linear mechanism"
    )


def test_design_lesson_mapping_retains_retrieval_and_applicability() -> None:
    export = build_long_term_export(_source())
    lesson = export.design_lessons[0]["lesson"]

    assert lesson["problem"] == "Problem 0"
    assert "Correction 0" in lesson["decision"]
    assert "Prevention 0" in lesson["prevention_action"]
    assert lesson["search_terms"] == ["lesson-term-0"]
    assert lesson["applicability"] == {"summary": "printed carriers"}
    assert lesson["non_applicable_conditions"] == ["metal welded carrier"]


def test_export_is_deterministic_under_source_row_reordering() -> None:
    source = _source()
    reversed_source = {key: list(reversed(value)) for key, value in source.items()}

    first = build_long_term_export(source)
    second = build_long_term_export(reversed_source)

    assert first.sha256 == second.sha256
    assert first.as_dict() == second.as_dict()


def test_dangling_family_reference_fails_closed() -> None:
    source = deepcopy(_source())
    source["knowledge_assertions"][0]["family_id"] = "missing-family"

    with pytest.raises(ValueError, match="missing-family"):
        build_long_term_export(source)


def test_unknown_source_collection_fails_closed() -> None:
    source = _source()
    source["design_jobs"] = []

    with pytest.raises(ValueError, match="unexpected source collections"):
        build_long_term_export(source)


def test_parity_probes_cover_family_alias_assertion_terms_and_lessons() -> None:
    probes = build_parity_probes(build_long_term_export(_source()))
    identities = {(probe.kind, probe.query, probe.expected_id) for probe in probes}

    assert ("product_family", "PF pilot", "PF-PILOT-001") in identities
    assert ("knowledge_assertion", "guide rail", "assertion-00") in identities
    assert ("design_lesson", "lesson-term-0", "lesson-lesson-key-0") in identities
